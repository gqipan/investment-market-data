"""Independent, method-preserving local valuation snapshots (not price releases)."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
from zoneinfo import ZoneInfo

from .privacy import scan_privacy
from .release import _json_bytes, dataset_identity

FIELDS = ('Date', 'Metric', 'Value', 'Unit', 'Method')
# Metric names identify definitions; provider-reported PE must not acquire a TTM label.
DEFINITIONS = {
    'pe_ttm': ('multiple', 'aggregate_ttm'),
    'pe_static': ('multiple', 'aggregate_static'),
    'pe_ttm_equal_weight': ('multiple', 'equal_weight'),
    'pe_static_equal_weight': ('multiple', 'equal_weight'),
    'pe_ttm_median': ('multiple', 'median'),
    'pe_static_median': ('multiple', 'median'),
    'pb': ('multiple', 'aggregate'),
    'pb_equal_weight': ('multiple', 'equal_weight'),
    'pb_median': ('multiple', 'median'),
    'pe_reported': ('multiple', 'provider_reported'),
    'pe_csindex_reported': ('multiple', 'api_peg'),
    'pb_reported': ('multiple', 'provider_reported'),
    'dividend_yield': ('fraction', 'provider_reported'),
    'pe_percentile': ('fraction', 'provider_window'),
    'pb_percentile': ('fraction', 'provider_window'),
}
LEGU_FIELDS = {
    '滚动市盈率': 'pe_ttm', '静态市盈率': 'pe_static',
    '等权滚动市盈率': 'pe_ttm_equal_weight', '等权静态市盈率': 'pe_static_equal_weight',
    '滚动市盈率中位数': 'pe_ttm_median', '静态市盈率中位数': 'pe_static_median',
    '市净率': 'pb', '等权市净率': 'pb_equal_weight', '市净率中位数': 'pb_median',
}


def _observation(day, metric, value):
    unit, method = DEFINITIONS[metric]
    return dict(zip(FIELDS, (day, metric, float(value), unit, method)))


def normalize_legu(pe: list[dict], pb: list[dict]) -> list[dict]:
    rows = []
    for record in pe + pb:
        day = str(record['日期'])[:10]
        for field, metric in LEGU_FIELDS.items():
            value = record.get(field)
            if value is not None:
                rows.append(_observation(day, metric, value))
    rows.sort(key=lambda row: (row['Date'], row['Metric']))
    validate_observations(rows)
    return rows


def normalize_danjuan(payload: dict, symbol: str) -> list[dict]:
    data = payload.get('data') if isinstance(payload, dict) else None
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError('Danjuan snapshot items are missing')
    matched = [item for item in items if item.get('index_code') == symbol]
    if not matched:
        return []
    if len(matched) != 1:
        raise ValueError('duplicate Danjuan index')
    item = matched[0]
    try:
        day = datetime.fromtimestamp(float(item['ts']) / 1000, ZoneInfo('Asia/Shanghai')).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise ValueError('invalid Danjuan timestamp') from exc
    fields = {'pe': 'pe_reported', 'pb': 'pb_reported', 'yeild': 'dividend_yield',
              'pe_percentile': 'pe_percentile', 'pb_percentile': 'pb_percentile'}
    rows = [_observation(day, metric, item[field]) for field, metric in fields.items()
            if item.get(field) is not None]
    rows.sort(key=lambda row: (row['Date'], row['Metric']))
    validate_observations(rows)
    return rows


def normalize_csindex(payload: dict, symbol: str) -> list[dict]:
    """Optional CSI perf field peg; retain its method separately from Legu TTM.

    AkShare labels this column rolling PE; the API name and method remain explicit.
    Null historical values are absent observations, never zero-filled.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise ValueError('invalid optional CSI history payload')
    rows = []
    for item in payload['data']:
        if not isinstance(item, dict) or item.get('indexCode') != symbol:
            raise ValueError('optional CSI history symbol mismatch')
        if item.get('peg') is None:
            continue
        raw_day = str(item['tradeDate'])
        day = datetime.strptime(raw_day, '%Y%m%d').date().isoformat()
        rows.append(_observation(day, 'pe_csindex_reported', item['peg']))
    rows.sort(key=lambda row: (row['Date'], row['Metric']))
    validate_observations(rows)
    return rows


def validate_observations(rows: list[dict]) -> None:
    if not rows:
        raise ValueError('no valuation observations')
    keys = []
    for row in rows:
        if set(row) != set(FIELDS):
            raise ValueError('invalid valuation fields')
        day = date.fromisoformat(row['Date'])
        if day.isoformat() != row['Date']:
            raise ValueError('noncanonical valuation date')
        metric = row['Metric']
        if metric not in DEFINITIONS or (row['Unit'], row['Method']) != DEFINITIONS[metric]:
            raise ValueError('unknown valuation metric, unit or method')
        value = float(row['Value'])
        if not math.isfinite(value):
            raise ValueError('valuation must be finite')
        if row['Unit'] == 'fraction' and not 0 <= value <= 1:
            raise ValueError('valuation fraction must be between zero and one')
        keys.append((row['Date'], metric))
    if len(keys) != len(set(keys)) or keys != sorted(keys):
        raise ValueError('valuation dates and metrics must be sorted and unique')


def metric_coverage(rows: list[dict], sessions: list[str] | None = None) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[row['Metric']].append(row['Date'])
    result = {}
    for metric, days in sorted(groups.items()):
        gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(days, days[1:])]
        frequency = 'snapshot' if len(days) == 1 else 'irregular_observed'
        if len(days) >= 3 and len({day[:7] for day in days}) == len(days) and 25 <= statistics.median(gaps) <= 35:
            frequency = 'monthly_observed'
        if len(days) > 1 and sessions is not None and set(days) == {day for day in sessions if days[0] <= day <= days[-1]}:
            frequency = 'daily_calendar_verified'
        result[metric] = {
            'row_count': len(days), 'first_date': days[0], 'last_date': days[-1],
            'frequency': frequency, 'median_gap_days': statistics.median(gaps) if gaps else None,
            'maximum_gap_days': max(gaps) if gaps else None,
            'requested_sessions_covered': set(days) == set(sessions) if sessions is not None else None,
            'missing_sessions': sorted(set(sessions) - set(days)) if sessions is not None else None,
            'unexpected_dates': sorted(set(days) - set(sessions)) if sessions is not None else None,
            'unit': DEFINITIONS[metric][0], 'method': DEFINITIONS[metric][1],
        }
    return result


def write_valuation(output: Path, rows: list[dict], *, symbol: str, source: str,
                    raw: dict, requested_start: str, requested_end: str,
                    library_version: str | None = None, sessions: list[str] | None = None) -> Path:
    output = output.resolve()
    if output.exists():
        raise FileExistsError('valuation snapshot already exists')
    first, last = date.fromisoformat(requested_start), date.fromisoformat(requested_end)
    validate_observations(rows)
    if first > last or any(not requested_start <= row['Date'] <= requested_end for row in rows):
        raise ValueError('valuation outside requested range')
    if not re.fullmatch(r'\d{6}', symbol) or source not in {'akshare_legulegu', 'danjuan_snapshot', 'csindex_optional_history'}:
        raise ValueError('unregistered valuation source or symbol')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.valuation-', dir=output.parent) as temporary:
        staging = Path(temporary)
        with (staging / 'observations.csv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        (staging / 'raw.json').write_bytes(_json_bytes(raw))
        meta = {'schema_version': 'valuation-local-1', 'data_class': 'valuation_observation',
                'symbol': symbol, 'source': source, 'synthetic': False,
                'requested_start': requested_start, 'requested_end': requested_end,
                'library_version': library_version, 'metrics': metric_coverage(rows, sessions),
                'calendar_sessions': sessions, 'publication_eligible': False, 'published_at': None,
                'downloaded_at': datetime.now(timezone.utc).isoformat(),
                'warnings': ['Local research only; no point-in-time publication evidence.',
                             'Provider methods stay separate; no daily forward filling.',
                             'Zero values are preserved as reported; applicability is not inferred.'],
                'files': {name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
                          for name in ('observations.csv', 'raw.json')}}
        meta['dataset_id'] = dataset_identity(meta)
        (staging / 'manifest.json').write_bytes(_json_bytes(meta))
        verify_valuation(staging)
        findings = scan_privacy(staging)
        if findings:
            raise ValueError('valuation privacy check failed: ' + '; '.join(findings))
        if output.exists():
            raise FileExistsError('valuation snapshot already exists')
        os.rename(staging, output)
    return output


def verify_valuation(path: Path) -> dict:
    if path.is_symlink() or any((path / name).is_symlink() for name in ('manifest.json', 'observations.csv', 'raw.json')):
        raise ValueError('valuation symlinks are forbidden')
    meta = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    if meta.get('schema_version') != 'valuation-local-1' or meta.get('data_class') != 'valuation_observation':
        raise ValueError('unknown valuation schema')
    if meta.get('dataset_id') != dataset_identity(meta):
        raise ValueError('valuation identity mismatch')
    if set(meta['files']) != {'observations.csv', 'raw.json'}:
        raise ValueError('unexpected valuation files')
    for name, expected in meta['files'].items():
        if hashlib.sha256((path / name).read_bytes()).hexdigest() != expected:
            raise ValueError('valuation file hash mismatch')
    with (path / 'observations.csv').open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    validate_observations(rows)
    if any(not meta['requested_start'] <= row['Date'] <= meta['requested_end'] for row in rows):
        raise ValueError('valuation outside requested range')
    if metric_coverage(rows, meta['calendar_sessions']) != meta['metrics']:
        raise ValueError('valuation coverage mismatch')
    if meta.get('publication_eligible') is not False or meta.get('published_at') is not None:
        raise ValueError('local valuation cannot claim publication eligibility')
    return meta
