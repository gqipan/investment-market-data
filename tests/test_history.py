"""History backfill must preserve gaps, source gates and successful siblings."""
import calendar
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class HistoryTests(unittest.TestCase):
    def test_cache_checks_body_hash_and_only_calls_public_allowlist(self):
        from market_data.history import CachedRequest
        from market_data.collection import TENCENT_URL
        calls = []
        def request(url, params):
            calls.append(params)
            return {'code': 0, 'data': {}}
        with tempfile.TemporaryDirectory() as temp:
            cache = CachedRequest(Path(temp), request)
            cache(TENCENT_URL, {'param': 'synthetic'})
            cache(TENCENT_URL, {'param': 'synthetic'})
            self.assertEqual(len(calls), 1)
            file = next(Path(temp).glob('*.json'))
            content = json.loads(file.read_text())
            content['payload']['code'] = 100
            file.write_text(json.dumps(content))
            with self.assertRaisesRegex(ValueError, 'cache hash'):
                cache(TENCENT_URL, {'param': 'synthetic'})
            with self.assertRaises(ValueError):
                cache('https://example.invalid/private', {})

    def test_history_reports_prefix_and_rejects_internal_gaps(self):
        from market_data.history import collect_history_price, CachedRequest
        from market_data.collection import CALENDAR_URL
        def request(url, params):
            if url == CALENDAR_URL:
                year, month = map(int, params['month'].split('-'))
                return {'data': [{'jyrq': date(year, month, day).isoformat(),
                                  'jybz': '1' if day in {2, 3} else '0'}
                                 for day in range(1, calendar.monthrange(year, month)[1] + 1)]}
            symbol, _, first, last, *_ = params['param'].split(',')
            if first == '2025-01-02':
                raise ValueError('must reuse validated annual raw response')
            rows = [] if first.startswith('2024') else [
                ['2025-01-02', '100', '101', '102', '99', '1000'],
                ['2025-01-03', '101', '102', '103', '100', '2000']]
            return {'code': 0, 'data': {symbol: {'day': rows}}}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            cache = CachedRequest(Path(temp) / 'cache', request)
            report = collect_history_price(root=ROOT, dataset_name='csi300-normalized-v1',
                start='2024-01-01', end='2025-01-03', output=Path(temp) / 'price', request=cache)
            self.assertEqual(report['first_date'], '2025-01-02')
            self.assertFalse(report['requested_range_complete'])
            self.assertTrue(report['coverage_verified'])
            self.assertEqual(report['row_count'], 2)
            self.assertEqual(report['prefix_unavailable_before'], '2025-01-02')
            with self.assertRaises(ValueError):
                collect_history_price(root=ROOT, dataset_name='csi300-normalized-v1',
                    start='2025-01-02', end='2025-01-03', output=Path(temp) / 'broken',
                    request=lambda url, params: request(url, params) if url == CALENDAR_URL else
                    {'code': 0, 'data': {'sh000300': {'day': [['2025-01-02', '1', '1', '1', '1', '0']]}}})
            self.assertFalse((Path(temp) / 'broken').exists())

    def test_cloud_gate_precedes_network_and_output(self):
        from market_data.history import backfill
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            with self.assertRaisesRegex(ValueError, 'cloud'):
                backfill(root=ROOT, start='2025-01-01', end='2025-01-03', output=Path(temp) / 'run',
                         request=lambda *_: self.fail('network'))
            self.assertFalse((Path(temp) / 'run').exists())

    def test_partial_backfill_retains_all_index_failures(self):
        from market_data.history import backfill
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            def unavailable(*args, **kwargs):
                raise ValueError('synthetic source unavailable')
            report = backfill(root=ROOT, start='2025-01-01', end='2025-01-03', output=Path(temp) / 'run',
                              request=unavailable, legu_fetch=unavailable, price_fetch=unavailable)
            self.assertEqual(report['status'], 'partial')
            self.assertEqual(len(report['indices']), 12)
            self.assertTrue(all(row['price']['status'] == 'unavailable' for row in report['indices']))
            self.assertTrue((Path(temp) / 'run' / 'coverage-report.md').is_file())

    def test_history_uses_the_dataset_source_instead_of_implicit_tencent(self):
        from market_data.history import collect_history_price
        from market_data.collection import CALENDAR_URL

        def request(url, params):
            if url == CALENDAR_URL:
                return {
                    'data': [
                        {
                            'jyrq': f'2018-06-{day:02d}',
                            'jybz': '1' if day == 15 else '0',
                        }
                        for day in range(1, 31)
                    ]
                }
            return {
                'code': '200',
                'data': [
                    {
                        'tradeDate': '20180615',
                        'open': 100,
                        'high': 102,
                        'low': 99,
                        'close': 101,
                        'tradingVol': 1000,
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {'GITHUB_ACTIONS': 'false'}
        ):
            report = collect_history_price(
                root=ROOT,
                dataset_name='csi-consumer-price-v1',
                start='2018-06-15',
                end='2018-06-15',
                output=Path(temp) / 'price',
                request=request,
            )
            self.assertEqual(report['source'], 'csindex_official')
            self.assertTrue(report['requested_range_complete'])


class HistoryCliTests(unittest.TestCase):
    def test_backfill_cli_reports_partial_with_exit_one(self):
        from market_data.cli import main
        from contextlib import redirect_stdout
        import io
        report = {'status': 'partial'}
        output = io.StringIO()
        with patch('market_data.history.backfill', return_value=report) as run, redirect_stdout(output):
            code = main(['backfill', '--start', '2025-01-01', '--end', '2025-01-03', '--output', '/tmp/synthetic-run'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), report)
        self.assertTrue(run.called)

class CalendarErratumTests(unittest.TestCase):
    def test_only_documented_closed_day_can_be_supplemented(self):
        from market_data.history import HistoricalCalendarRequest
        from market_data.collection import CALENDAR_URL, fetch_calendar
        def request(url, params):
            return {'data': [{'jyrq': f'2017-01-{day:02d}', 'jybz': '1' if day == 3 else '0'}
                             for day in range(2, 32)]}
        fixed = HistoricalCalendarRequest(request)
        sessions = fetch_calendar(date(2017, 1, 1), date(2017, 1, 31), request=fixed)
        self.assertEqual(sessions, [date(2017, 1, 3)])
        self.assertEqual(fixed.repairs, ['2017-01-01'])
        original = request(CALENDAR_URL, {'month': '2017-01'})
        self.assertEqual(len(original['data']), 30)
        missing_weekday = HistoricalCalendarRequest(lambda *_: {'data': original['data'][1:]})
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            fetch_calendar(date(2017, 1, 1), date(2017, 1, 31), request=missing_weekday)

class FailureIsolationTests(unittest.TestCase):
    def test_error_report_does_not_retain_raw_exception_text(self):
        from market_data.history import _error
        report = _error(ValueError('/some/absolute/location/bars.csv: invalid OHLC relationship'))
        self.assertNotIn('/some/', json.dumps(report))
        self.assertIn('reason_code', report)

    def test_malformed_annual_bar_is_value_error(self):
        from market_data.history import collect_history_price
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            with self.assertRaises(ValueError):
                collect_history_price(root=ROOT, dataset_name='csi300-normalized-v1',
                    start='2025-01-01', end='2025-01-03', output=Path(temp)/'run',
                    request=lambda *_: {'code': 0, 'data': {'sh000300': {'day': [[]]}}})

    def test_bad_ohlc_keeps_independently_validated_close_history(self):
        from market_data.history import collect_history_price
        from market_data.collection import CALENDAR_URL
        def request(url, params):
            if url == CALENDAR_URL:
                return {'data': [{'jyrq': f'2025-01-{day:02d}', 'jybz': '1' if day in {2, 3} else '0'}
                                 for day in range(1, 32)]}
            return {'code': 0, 'data': {'sh000300': {'day': [
                ['2025-01-02', '100', '101', '102', '99', '1000'],
                ['2025-01-03', '101', '102', '105', '105', '2000']]}}}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            path = Path(temp)/'price'
            report = collect_history_price(root=ROOT, dataset_name='csi300-normalized-v1',
                start='2025-01-02', end='2025-01-03', output=path, request=request)
            self.assertEqual(report['status'], 'close_only')
            self.assertEqual(report['row_count'], 2)
            self.assertEqual(report['ohlc_anomalies'][0]['date'], '2025-01-03')
            self.assertTrue(report['coverage_verified'])
            self.assertFalse((path/'bars.csv').exists())
            self.assertEqual((path/'close.csv').read_text().splitlines()[-1], '2025-01-03,102')

class CloseHistoryTests(unittest.TestCase):
    def test_gapped_close_retains_dates_but_cannot_pass_coverage(self):
        from market_data.history import _write_close_history, verify_close_history
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'close'
            meta=_write_close_history(path,[['2025-01-02','100','101','105','105','1000']],
                sessions=['2025-01-02','2025-01-03'],symbol='000300',requested_start='2025-01-02',
                requested_end='2025-01-03',calendar_errata=[])
            self.assertFalse(meta['coverage_verified'])
            self.assertEqual(meta['missing_sessions'], ['2025-01-03'])
            verify_close_history(path)
            (path/'close.csv').write_text('Date,Close\n2025-01-02,999\n')
            with self.assertRaisesRegex(ValueError,'hash'):
                verify_close_history(path)


class BackfillBoundaryTests(unittest.TestCase):
    def test_malformed_calendar_still_writes_partial_report(self):
        from market_data.history import backfill
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS':'false'}):
            def unavailable(**kwargs):raise ValueError('unavailable')
            report=backfill(root=ROOT,start='2017-01-01',end='2017-01-03',output=Path(temp)/'run',
                request=lambda *_:{'data':[None]},price_fetch=unavailable,legu_fetch=lambda *_:{'pe':[],'pb':[]})
            self.assertEqual(report['status'],'partial')
            self.assertEqual(report['calendar']['status'],'unavailable')
            self.assertEqual(len(report['indices']),12)
            self.assertTrue((Path(temp)/'run/coverage-report.json').exists())

    def test_metadata_write_failure_does_not_install_snapshot(self):
        from market_data import history
        from market_data.collection import CALENDAR_URL
        from market_data.release import _json_bytes
        def request(url, params):
            if url==CALENDAR_URL:
                return {'data':[{'jyrq':f'2025-01-{day:02d}','jybz':'1' if day==2 else '0'} for day in range(1,32)]}
            return {'code':0,'data':{'sh000300':{'day':[['2025-01-02','100','101','102','99','1000']]}}}
        def encode(value):
            if value.get('adapter_version')=='history-price-v1':raise OSError('synthetic disk failure')
            return _json_bytes(value)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS':'false'}):
            path=Path(temp)/'price'
            with patch('market_data.history._json_bytes',side_effect=encode),self.assertRaises(OSError):
                history.collect_history_price(root=ROOT,dataset_name='csi300-normalized-v1',
                    start='2025-01-02',end='2025-01-02',output=path,request=request)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
