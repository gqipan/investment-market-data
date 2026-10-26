"""Synthetic valuation observations; never infer daily history from snapshots."""
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest


class ValuationTests(unittest.TestCase):
    def test_monthly_history_keeps_methods_and_missing_values(self):
        from market_data.valuation import normalize_legu, write_valuation, verify_valuation
        pe = [{"日期": f"2025-{month:02d}-28", "滚动市盈率": 12 + month,
               "静态市盈率": 14, "等权滚动市盈率": 20, "滚动市盈率中位数": None}
              for month in range(1, 5)]
        rows = normalize_legu(pe, [])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'value'
            write_valuation(path, rows, symbol='000300', source='akshare_legulegu',
                            raw={'pe': pe, 'pb': []}, requested_start='2025-01-01',
                            requested_end='2025-04-30', library_version='test')
            meta = verify_valuation(path)
            self.assertEqual(meta['metrics']['pe_ttm']['frequency'], 'monthly_observed')
            self.assertEqual(meta['metrics']['pe_ttm']['row_count'], 4)
            self.assertNotIn('pe_ttm_median', meta['metrics'])
            self.assertNotIn('pb', meta['metrics'])
            self.assertFalse(meta['publication_eligible'])
            self.assertIsNone(meta['published_at'])
            self.assertNotEqual(meta['metrics']['pe_ttm']['method'], meta['metrics']['pe_static']['method'])
            (path / 'observations.csv').write_text('corrupt')
            with self.assertRaisesRegex(ValueError, 'hash'):
                verify_valuation(path)

    def test_h30269_valuation_roundtrip_preserves_identity(self):
        from market_data.valuation import write_valuation, verify_valuation
        row = {'Date': '2026-10-26', 'Metric': 'pe_reported', 'Value': 9,
               'Unit': 'multiple', 'Method': 'provider_reported'}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'value'
            write_valuation(target, [row], symbol='H30269', source='danjuan_snapshot', raw={},
                            requested_start='2026-10-26', requested_end='2026-10-26')
            self.assertEqual(verify_valuation(target)['symbol'], 'H30269')
            self.assertEqual(verify_valuation(target)['metrics']['pe_reported']['frequency'], 'snapshot')

    def test_reject_duplicate_nonfinite_wrong_units_and_method(self):
        from market_data.valuation import validate_observations
        row = {'Date': '2025-01-02', 'Metric': 'dividend_yield', 'Value': .03,
               'Unit': 'fraction', 'Method': 'provider_reported'}
        validate_observations([row])
        for rows in ([row, row], [{**row, 'Value': float('nan')}],
                     [{**row, 'Unit': 'percent'}], [{**row, 'Method': 'guessed'}],
                     [{**row, 'Value': -1}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_observations(rows)

    def test_danjuan_uses_timestamp_and_fraction_without_claiming_ttm(self):
        from market_data.valuation import normalize_danjuan
        payload = {'data': {'items': [{'index_code': 'SH000300', 'ts': 1735776000000,
            'pe': 12, 'pb': 1.2, 'yeild': .03, 'pe_percentile': 0, 'pb_percentile': .5}]}}
        rows = normalize_danjuan(payload, 'SH000300')
        by_metric = {row['Metric']: row for row in rows}
        self.assertEqual(by_metric['dividend_yield']['Value'], .03)
        self.assertEqual(by_metric['pe_percentile']['Value'], 0)
        self.assertEqual(by_metric['pe_reported']['Date'], '2025-01-02')
        self.assertNotIn('pe_ttm', by_metric)
        self.assertEqual(normalize_danjuan(payload, 'SH000510'), [])

    def test_range_and_metadata_tampering_are_rejected(self):
        from market_data.valuation import write_valuation, verify_valuation
        row = {'Date': '2025-01-02', 'Metric': 'pb', 'Value': 1,
               'Unit': 'multiple', 'Method': 'aggregate'}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'value'
            with self.assertRaisesRegex(ValueError, 'range'):
                write_valuation(path, [row], symbol='000300', source='akshare_legulegu',
                                raw={}, requested_start='2026-01-01', requested_end='2026-01-02')
            write_valuation(path, [row], symbol='000300', source='akshare_legulegu', raw={},
                            requested_start='2025-01-01', requested_end='2025-01-02')
            meta = json.loads((path / 'manifest.json').read_text())
            meta['metrics']['pb']['row_count'] = 999
            (path / 'manifest.json').write_text(json.dumps(meta))
            with self.assertRaises(ValueError):
                verify_valuation(path)
            with self.assertRaises(FileExistsError):
                write_valuation(path, [row], symbol='000300', source='akshare_legulegu', raw={},
                                requested_start='2025-01-01', requested_end='2025-01-02')


class FrequencyTests(unittest.TestCase):
    def test_single_snapshot_cannot_become_daily_history(self):
        from market_data.valuation import metric_coverage
        rows = [{'Date': '2025-01-02', 'Metric': 'pe_reported', 'Value': 12,
                 'Unit': 'multiple', 'Method': 'provider_reported'}]
        self.assertEqual(metric_coverage(rows, ['2025-01-02'])['pe_reported']['frequency'], 'snapshot')

class OptionalCsindexTests(unittest.TestCase):
    def test_explicit_optional_peg_does_not_acquire_ttm_label(self):
        from market_data.valuation import normalize_csindex
        payload={'data':[{'tradeDate':'20250102','indexCode':'000300','peg':None},
                         {'tradeDate':'20250103','indexCode':'000300','peg':12.3}]}
        rows=normalize_csindex(payload,'000300')
        self.assertEqual(rows[0]['Metric'],'pe_csindex_reported')
        self.assertEqual(rows[0]['Date'],'2025-01-03')
        with self.assertRaises(ValueError):normalize_csindex(payload,'930955')

    def test_daily_observed_span_does_not_claim_requested_prefix(self):
        from market_data.valuation import metric_coverage
        rows=[{'Date':day,'Metric':'pe_ttm','Value':12,'Unit':'multiple','Method':'aggregate_ttm'}
              for day in ['2025-01-03','2025-01-06']]
        meta=metric_coverage(rows,['2025-01-02','2025-01-03','2025-01-06'])['pe_ttm']
        self.assertEqual(meta['frequency'],'daily_calendar_verified')
        self.assertFalse(meta['requested_sessions_covered'])
        self.assertEqual(meta['missing_sessions'],['2025-01-02'])

class MalformedSnapshotTests(unittest.TestCase):
    def test_bad_payload_and_timestamp_are_validation_errors(self):
        from market_data.valuation import normalize_danjuan
        for payload in ({'data': []}, {'data': {'items': [None]}},
                        {'data': {'items': [{'index_code':'SH000300','ts':1e300,'pe':1}]}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_danjuan(payload,'SH000300')


if __name__ == '__main__':
    unittest.main()
