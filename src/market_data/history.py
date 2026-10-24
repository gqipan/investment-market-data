"""Local multi-index history backfill with shared, hash-checked request caching."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import copy
import csv
import math
import tempfile
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from .collection import (
    CALENDAR_URL,
    CSINDEX_URL,
    SINA_URL,
    TENCENT_URL,
    _akshare_bars,
    collect_index,
    fetch_calendar,
    request_json,
)
from .release import _json_bytes, dataset_identity
from .valuation import normalize_csindex, normalize_danjuan, normalize_legu, write_valuation, verify_valuation

DANJUAN_URL = 'https://danjuanfunds.com/djapi/index_eva/dj'


def _local_gate(sources, source):
    policy = sources[source]
    if policy.get('status') == 'restricted' or policy.get('local_collection_allowed') is not True:
        raise ValueError('local research collection is not enabled for ' + source)
    cloud_allowed = policy.get('cloud_collection_allowed') is True or (
        os.environ.get('MARKET_DATA_PRIVATE_CLOUD') == 'true'
        and policy.get('private_cloud_collection_allowed') is True
    )
    if os.environ.get('GITHUB_ACTIONS') == 'true' and not cloud_allowed:
        raise ValueError('cloud collection is not approved for ' + source)


def _output_path(root, output):
    output = output.resolve()
    if output.is_relative_to(root) and not output.is_relative_to(root / 'data'):
        raise ValueError('history output must be outside the repository or under ignored data/')
    return output


class CachedRequest:
    """The cache detects accidental corruption; it is not trusted release evidence."""
    def __init__(self, directory: Path, request=request_json):
        self.directory, self.request = directory, request
        self.lock = threading.Lock()

    def __call__(self, url: str, params: dict):
        allowed = {TENCENT_URL: {'param'}, SINA_URL: {'symbol', 'scale', 'ma', 'datalen'},
                   CALENDAR_URL: {'month'}, DANJUAN_URL: set(),
                   CSINDEX_URL: {'indexCode', 'startDate', 'endDate'}}
        if url not in allowed or set(params) != allowed[url]:
            raise ValueError('request is outside the public history endpoint allowlist')
        descriptor = {'url': url, 'params': params}
        key = hashlib.sha256(_json_bytes(descriptor)).hexdigest()
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (key + '.json')
            if path.exists():
                if path.is_symlink():
                    raise ValueError('cache symlink is forbidden')
                cached = json.loads(path.read_text(encoding='utf-8'))
                if (cached['request'] != descriptor or hashlib.sha256(_json_bytes(cached['payload'])).hexdigest()
                        != cached['sha256']):
                    raise ValueError('cache hash or request mismatch')
                return cached['payload']
            payload = self.request(url, params)
            entry = {'request': descriptor, 'payload': payload,
                     'sha256': hashlib.sha256(_json_bytes(payload)).hexdigest(),
                     'downloaded_at': datetime.now(timezone.utc).isoformat()}
            with path.open('x', encoding='utf-8') as handle:
                handle.write(_json_bytes(entry).decode('utf-8'))
            return payload


CALENDAR_ERRATUM_URL = 'https://www.szse.cn/disclosure/notice/general/t20060515_499577.html'


class HistoricalCalendarRequest:
    """Apply only the observed, documented 2017-01-01 Sunday omission.

    Raw cache remains unchanged. Unknown omissions still fail strict validation.
    SZSE trading rules 2.3.1 limit trading days to Monday through Friday.
    """
    def __init__(self, request):
        self.request = request
        self.repairs = []
        self.lock = threading.Lock()

    def __call__(self, url, params):
        payload = self.request(url, params)
        if url == CALENDAR_URL and params == {'month': '2017-01'} and isinstance(payload, dict):
            rows = payload.get('data')
            if isinstance(rows, list) and any(not isinstance(row, dict) for row in rows):
                raise ValueError('invalid historical calendar row')
            if isinstance(rows, list) and rows and not any(str(row.get('jyrq', ''))[:10] == '2017-01-01' for row in rows):
                payload = copy.deepcopy(payload)
                payload['data'].append({'jyrq': '2017-01-01', 'jybz': '0'})
                with self.lock:
                    if '2017-01-01' not in self.repairs:
                        self.repairs.append('2017-01-01')
        return payload


def collect_history_price(*, root: Path, dataset_name: str, start: str, end: str,
                          output: Path, request=request_json) -> dict:
    """Discover an available prefix, then strictly validate through requested end."""
    root = root.resolve()
    output = _output_path(root, output)
    if output.exists():
        raise FileExistsError('history snapshot already exists')
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError('start must not follow end')
    datasets = json.loads((root / 'catalog/datasets.json').read_text())
    dataset = datasets[dataset_name]
    instruments = json.loads((root / 'catalog/instruments.json').read_text())
    instrument = instruments[dataset['instrument']]
    provider = dataset['source']
    sources = json.loads((root / 'catalog/sources.json').read_text())
    for source in (provider, 'szse_calendar'):
        _local_gate(sources, source)
    symbol = instrument['provider_symbols'][provider]
    if not isinstance(request, HistoricalCalendarRequest):
        request = HistoricalCalendarRequest(request)
    if provider == 'csindex_official':
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.history-', dir=output.parent) as temporary:
            staged = Path(temporary) / 'snapshot'
            collect_index(
                root=root,
                dataset_name=dataset_name,
                provider=provider,
                start=start,
                end=end,
                output=staged,
                request=request,
            )
            meta = json.loads((staged / 'manifest.json').read_text())
            meta['calendar_errata'] = {
                'closed_dates': [
                    day for day in request.repairs if start <= day <= end
                ],
                'basis_url': CALENDAR_ERRATUM_URL,
            }
            meta['adapter_version'] = 'history-price-v1'
            meta['history_requested_start'] = start
            meta['history_requested_end'] = end
            meta['dataset_id'] = dataset_identity(meta)
            (staged / 'manifest.json').write_bytes(_json_bytes(meta))
            if output.exists():
                raise FileExistsError('history snapshot already exists')
            os.rename(staged, output)
        return {
            'status': 'available',
            'source': provider,
            'path': str(output.name),
            'first_date': meta['first_date'],
            'last_date': meta['last_date'],
            'row_count': meta['row_count'],
            'frequency': 'daily_calendar_verified',
            'coverage_verified': True,
            'requested_start': start,
            'requested_end': end,
            'requested_range_complete': True,
            'prefix_unavailable_before': None,
            'calendar_errata': meta['calendar_errata'],
            'ohlc_anomalies': [],
            'ohlcv_validation_passed': True,
            'missing_sessions': [],
            'dataset_id': meta['dataset_id'],
        }
    days = []
    annual_payloads = {}
    if provider == 'tencent_raw':
        for year in range(first.year, last.year + 1):
            begin, finish = max(first, date(year, 1, 1)), min(last, date(year, 12, 31))
            payload = request(TENCENT_URL, {'param': f'{symbol},day,{begin},{finish},640,'})
            if not isinstance(payload, dict) or payload.get('code') != 0:
                raise ValueError('Tencent history response failed')
            data = payload.get('data')
            if not isinstance(data, dict) or not isinstance(data.get(symbol, {}), dict):
                raise ValueError('invalid Tencent history container')
            rows = data.get(symbol, {}).get('day', [])
            if not isinstance(rows, list):
                raise ValueError('invalid Tencent history rows')
            annual_payloads[year] = payload
            for row in rows:
                if not isinstance(row, list) or len(row) < 6:
                    raise ValueError('incomplete Tencent history bar')
                day = date.fromisoformat(row[0])
                if not begin <= day <= finish:
                    raise ValueError('history date outside annual request')
                days.append(day)
    else:
        rows, _ = _akshare_bars(symbol, first, last)
        days = [date.fromisoformat(str(row['date'])[:10]) for row in rows]
        if any(not first <= day <= last for day in days):
            raise ValueError('history date outside request')
    if not days:
        raise ValueError('no raw history in requested range')
    observed_start = min(days).isoformat()
    # Reuse the exact annual responses already acquired; never refetch a different revision.
    def snapshot_request(url, params):
        if url != TENCENT_URL:
            return request(url, params)
        requested_symbol, _, begin, finish, *_ = params['param'].split(',')
        if requested_symbol != symbol:
            raise ValueError('unexpected history symbol')
        payload = copy.deepcopy(annual_payloads[date.fromisoformat(begin).year])
        rows = payload.get('data', {}).get(symbol, {}).get('day', [])
        if not rows:
            raise ValueError('Tencent raw day series is missing inside available history')
        payload['data'][symbol]['day'] = [row for row in rows if begin <= row[0] <= finish]
        return payload
    sessions = fetch_calendar(first, last, request=request)
    expected_start = sessions[0].isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.history-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'snapshot'
        try:
            collect_index(root=root, dataset_name=dataset_name, provider=provider,
                          start=observed_start, end=end, output=staged, request=snapshot_request)
            meta = json.loads((staged / 'manifest.json').read_text())
            meta['calendar_errata'] = {'closed_dates': [day for day in request.repairs if observed_start <= day <= end],
                                       'basis_url': CALENDAR_ERRATUM_URL}
            meta['adapter_version'] = 'history-price-v1'
            meta['history_requested_start'] = start
            meta['history_requested_end'] = end
            meta['dataset_id'] = dataset_identity(meta)
            (staged / 'manifest.json').write_bytes(_json_bytes(meta))
            status = 'available'
            anomalies = []
        except ValueError as exc:
            if provider != 'tencent_raw' or 'invalid OHLC relationship' not in str(exc):
                raise
            records = [row for year in sorted(annual_payloads)
                       for row in annual_payloads[year].get('data', {}).get(symbol, {}).get('day', [])]
            meta = _write_close_history(staged, records, sessions=[day.isoformat() for day in sessions
                if day.isoformat() >= observed_start], symbol=instrument['symbol'],
                requested_start=start, requested_end=end, calendar_errata=request.repairs)
            status = 'close_only' if meta['coverage_verified'] else 'gapped_close'
            anomalies = meta['ohlc_anomalies']
        if output.exists():
            raise FileExistsError('history snapshot already exists')
        os.rename(staged, output)
    return {'status': status, 'source': provider, 'path': str(output.name),
            'first_date': meta['first_date'], 'last_date': meta['last_date'], 'row_count': meta['row_count'],
            'frequency': 'daily_calendar_verified' if meta['coverage_verified'] else 'gapped_daily',
            'coverage_verified': meta['coverage_verified'],
            'requested_start': start, 'requested_end': end,
            'requested_range_complete': observed_start == expected_start and meta['coverage_verified'],
            'prefix_unavailable_before': observed_start if observed_start != expected_start else None,
            'calendar_errata': meta['calendar_errata'], 'ohlc_anomalies': anomalies,
            'ohlcv_validation_passed': status == 'available',
            'missing_sessions': meta.get('missing_sessions', []), 'dataset_id': meta['dataset_id']}


def _write_close_history(output, records, *, sessions, symbol, requested_start, requested_end, calendar_errata):
    """Export actual Close only; never synthesize OHLC around damaged source bars."""
    days, closes, anomalies = [], [], []
    for row in records:
        day = date.fromisoformat(row[0]).isoformat()
        values = list(map(float, row[1:6]))
        open_, close, high, low, volume = values
        if not all(math.isfinite(value) for value in values) or min(open_, close, high, low) <= 0 or volume < 0:
            raise ValueError('invalid raw history values')
        if high < max(open_, close, low) or low > min(open_, close, high):
            anomalies.append({'date': day, 'reason_code': 'invalid_ohlc_relationship',
                              'raw_open_close_high_low_volume': row[1:6]})
        days.append(day)
        closes.append(close)
    if not days or days != sorted(set(days)) or set(days) - set(sessions):
        raise ValueError('close history trading-session coverage mismatch')
    missing = sorted(set(sessions) - set(days))
    output.mkdir()
    with (output / 'close.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Date', 'Close'])
        writer.writerows((day, format(close, '.15g')) for day, close in zip(days, closes))
    (output / 'raw-bars.json').write_bytes(_json_bytes({'rows': records}))
    meta = {'schema_version': 'close-history-local-1', 'data_class': 'normalized_close',
            'source': 'tencent_raw', 'source_url': TENCENT_URL, 'symbol': symbol,
            'synthetic': False, 'adjust': 'raw', 'currency': 'CNY', 'market': 'CN',
            'first_date': days[0], 'last_date': days[-1], 'row_count': len(days),
            'requested_start': requested_start, 'requested_end': requested_end,
            'expected_sessions': sessions, 'coverage_verified': not missing, 'missing_sessions': missing,
            'ohlcv_validation_passed': False, 'ohlc_anomalies': anomalies,
            'calendar_errata': {'closed_dates': [day for day in calendar_errata if days[0] <= day <= days[-1]],
                                'basis_url': CALENDAR_ERRATUM_URL},
            'downloaded_at': datetime.now(timezone.utc).isoformat(), 'published_at': None,
            'publication_eligible': False,
            'warning': 'Only source Close is validated. OHLCV snapshot rejected; do not feed this schema into the price release builder.',
            'files': {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                      for name in ('close.csv', 'raw-bars.json')}}
    meta['dataset_id'] = dataset_identity(meta)
    (output / 'manifest.json').write_bytes(_json_bytes(meta))
    verify_close_history(output)
    return meta


def verify_close_history(path: Path) -> dict:
    if path.is_symlink() or any((path / name).is_symlink() for name in ('manifest.json', 'close.csv', 'raw-bars.json')):
        raise ValueError('close history symlinks are forbidden')
    meta = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    if meta.get('schema_version') != 'close-history-local-1' or meta.get('data_class') != 'normalized_close':
        raise ValueError('unknown close history schema')
    if meta.get('dataset_id') != dataset_identity(meta):
        raise ValueError('close history identity mismatch')
    if set(meta['files']) != {'close.csv', 'raw-bars.json'}:
        raise ValueError('unexpected close history files')
    for name, expected in meta['files'].items():
        if hashlib.sha256((path / name).read_bytes()).hexdigest() != expected:
            raise ValueError('close history file hash mismatch')
    with (path / 'close.csv').open(encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ['Date', 'Close']:
            raise ValueError('invalid close history fields')
        rows = list(reader)
    days = [date.fromisoformat(row['Date']).isoformat() for row in rows]
    values = [float(row['Close']) for row in rows]
    if not days or days != sorted(set(days)) or any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('invalid close history observations')
    if set(days) - set(meta['expected_sessions']):
        raise ValueError('unexpected close history dates')
    missing = sorted(set(meta['expected_sessions']) - set(days))
    if (missing != meta['missing_sessions'] or meta['coverage_verified'] != (not missing)
            or len(rows) != meta['row_count'] or days[0] != meta['first_date'] or days[-1] != meta['last_date']):
        raise ValueError('close history coverage mismatch')
    if meta.get('publication_eligible') is not False or meta.get('ohlcv_validation_passed') is not False:
        raise ValueError('close history is not an approved OHLCV release')
    return meta


def fetch_legu(symbol: str) -> dict:
    try:
        result = subprocess.run([sys.executable, '-m', 'market_data.valuation_worker', symbol],
                                capture_output=True, text=True, check=True, timeout=180)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ValueError('AkShare PE/PB history unavailable or timed out; check collection extra and connectivity') from exc


def _error(exc):
    # Fixed codes prevent source/library exceptions from leaking paths or credentials.
    code = 'source_or_validation_error'
    for fragment, candidate in [('AkShare', 'akshare_unavailable'), ('no valuation', 'no_observations'),
            ('OHLC relationship', 'invalid_ohlc'), ('coverage mismatch', 'calendar_coverage_mismatch'),
            ('calendar month is incomplete', 'calendar_incomplete'), ('no raw history', 'no_raw_history'),
            ('source is unavailable', 'source_unavailable'), ('outside', 'out_of_range'),
            ('cache hash', 'cache_integrity_failed')]:
        if fragment in str(exc):
            code = candidate
            break
    return {'status': 'unavailable', 'reason_code': code, 'reason': code,
            'error_type': type(exc).__name__}


def backfill(*, root: Path, start: str, end: str, output: Path, request=request_json,
             legu_fetch=fetch_legu, price_fetch=collect_history_price, progress=None, include_csindex=False) -> dict:
    root = root.resolve()
    output = _output_path(root, output)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError('start must not follow end')
    if output.exists():
        raise FileExistsError('backfill run already exists; choose a new output directory')
    sources = json.loads((root / 'catalog/sources.json').read_text())
    for source in ('tencent_raw', 'akshare_eastmoney', 'szse_calendar', 'akshare_legulegu', 'danjuan_snapshot'):
        _local_gate(sources, source)
    if include_csindex:
        _local_gate(sources, 'csindex_optional_history')
    instruments = json.loads((root / 'catalog/instruments.json').read_text())
    datasets = json.loads((root / 'catalog/datasets.json').read_text())
    watchlist = json.loads((root / 'catalog/watchlist.json').read_text())['symbols']
    selected = [(key, item) for key, item in instruments.items() if item['symbol'] in watchlist]
    output.mkdir(parents=True)
    cached = HistoricalCalendarRequest(CachedRequest(output / 'request-cache', request))
    report = {'schema_version': 'history-coverage-1', 'status': 'partial',
              'requested_start': start, 'requested_end': end,
              'generated_at': datetime.now(timezone.utc).isoformat(), 'indices': [],
              'publication_eligible': False, 'promotion_allowed': False,
              'note': 'Local research only. Requested coverage, daily frequency and source publication rights are independent.'}
    try:
        sessions = [day.isoformat() for day in fetch_calendar(first, last, request=cached)]
        report['calendar'] = {'status': 'available', 'row_count': len(sessions),
                              'closed_date_errata': cached.repairs, 'basis_url': CALENDAR_ERRATUM_URL}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        sessions = None
        report['calendar'] = _error(exc)
    try:
        danjuan = cached(DANJUAN_URL, {})
    except (ValueError, KeyError, TypeError, OSError) as exc:
        danjuan = None
        report['danjuan'] = _error(exc)

    def one(selected_item):
        instrument_id, item = selected_item
        code = item['symbol']
        result = {'symbol': code, 'name': item['display_name'], 'valuations': []}
        dataset_name = next(key for key, ds in datasets.items()
                            if ds['instrument'] == instrument_id and ds['data_class'] == 'normalized_price')
        try:
            result['price'] = price_fetch(root=root, dataset_name=dataset_name, start=start, end=end,
                                           output=output / code / 'price', request=cached)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result['price'] = _error(exc)
        legu_symbol = item['provider_symbols'].get('akshare_legulegu')
        if legu_symbol:
            try:
                raw = legu_fetch(legu_symbol)
                rows = [row for row in normalize_legu(raw['pe'], raw['pb']) if start <= row['Date'] <= end]
                path = write_valuation(output / code / 'akshare_legulegu', rows, symbol=code,
                    source='akshare_legulegu', raw=raw, requested_start=start, requested_end=end,
                    library_version=raw.get('version'), sessions=sessions)
                meta = verify_valuation(path)
                result['valuations'].append({'status': 'available', 'source': 'akshare_legulegu',
                                             'metrics': meta['metrics'], 'dataset_id': meta['dataset_id']})
            except (ValueError, KeyError, TypeError, OSError) as exc:
                result['valuations'].append({'source': 'akshare_legulegu', **_error(exc)})
        else:
            result['valuations'].append({'source': 'akshare_legulegu', 'status': 'unsupported_mapping'})
        try:
            if danjuan is None:
                raise ValueError('Danjuan endpoint unavailable')
            rows = normalize_danjuan(danjuan, item['provider_symbols']['danjuan_snapshot'])
            rows = [row for row in rows if start <= row['Date'] <= end]
            path = write_valuation(output / code / 'danjuan_snapshot', rows, symbol=code,
                source='danjuan_snapshot', raw={'items': [entry for entry in danjuan['data']['items']
                    if entry.get('index_code') == item['provider_symbols']['danjuan_snapshot']]},
                requested_start=start, requested_end=end, sessions=sessions)
            meta = verify_valuation(path)
            result['valuations'].append({'status': 'available', 'source': 'danjuan_snapshot',
                                         'metrics': meta['metrics'], 'dataset_id': meta['dataset_id']})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result['valuations'].append({'source': 'danjuan_snapshot', **_error(exc)})
        if include_csindex:
            try:
                source_symbol = item['provider_symbols'].get('csindex_optional_history')
                if not source_symbol:
                    raise ValueError('optional CSI symbol is not registered')
                raw = cached(CSINDEX_URL, {'indexCode': source_symbol,
                    'startDate': start.replace('-', ''), 'endDate': end.replace('-', '')})
                rows = normalize_csindex(raw, code)
                path = write_valuation(output / code / 'csindex_optional_history', rows, symbol=code,
                    source='csindex_optional_history', raw=raw, requested_start=start, requested_end=end,
                    sessions=sessions)
                meta = verify_valuation(path)
                result['valuations'].append({'status': 'available', 'source': 'csindex_optional_history',
                    'metrics': meta['metrics'], 'dataset_id': meta['dataset_id']})
            except (ValueError, KeyError, TypeError, OSError) as exc:
                result['valuations'].append({'source': 'csindex_optional_history', **_error(exc)})
        result['missing_daily_history'] = []
        for label, metrics in [('pe', {'pe_ttm', 'pe_reported', 'pe_csindex_reported'}), ('pb', {'pb', 'pb_reported'}),
                               ('dividend_yield', {'dividend_yield'})]:
            covered = any(meta.get('status') == 'available' and any(
                metric in metrics and details['frequency'] == 'daily_calendar_verified' and details['requested_sessions_covered']
                for metric, details in meta['metrics'].items()) for meta in result['valuations'])
            if not covered:
                result['missing_daily_history'].append(label)
        if progress:
            progress(code, result)
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(one, selected):
            report['indices'].append(result)
            # Checkpoint successful siblings even when a later provider is slow.
            _save_report(output, report)
    if report['indices'] and all(row['price'].get('status') == 'available' and row['price'].get('requested_range_complete') and not row['missing_daily_history'] for row in report['indices']):
        report['status'] = 'complete'
    _save_report(output, report)
    return report


def _save_report(output: Path, report: dict):
    (output / 'coverage-report.json').write_bytes(_json_bytes(report))
    lines = ['# Local history coverage', '', f"Requested: {report['requested_start']} to {report['requested_end']}",
             '', f"Status: {report['status']}; local research only; publication/promotion disabled.", '',
             '| Index | Price | PE history | PB history | Dividend yield history |', '|---|---|---|---|---|']
    for item in report['indices']:
        price = item['price']
        price_text = (f"{price['row_count']} rows; {price['first_date']} .. {price['last_date']}"
                      if price['status'] in {'available', 'close_only', 'gapped_close'} else price.get('reason', price['status']))
        if price['status'] in {'close_only', 'gapped_close'}:
            price_text += f"; close only; OHLC anomalies={len(price['ohlc_anomalies'])}; missing sessions={len(price.get('missing_sessions', []))}"
        cells = []
        for metrics in ({'pe_ttm', 'pe_reported', 'pe_csindex_reported'}, {'pb', 'pb_reported'}, {'dividend_yield'}):
            values = []
            for source in item['valuations']:
                for metric, meta in source.get('metrics', {}).items():
                    if metric in metrics:
                        values.append(f"{source['source']}/{metric}: {meta['row_count']} rows; "
                                      f"{meta['first_date']} .. {meta['last_date']}; {meta['frequency']}")
            cells.append('<br>'.join(values) or 'unavailable')
        lines.append('| ' + ' | '.join([item['symbol'] + ' ' + item['name'], price_text, *cells]) + ' |')
    (output / 'coverage-report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
