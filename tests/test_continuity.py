"""Regressions for established same-source research routes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ContinuityTests(unittest.TestCase):
    def _price(self, path, *, start="2026-10-26", end="2026-10-26", source="tencent_raw", symbol="000922", close=102):
        from market_data.collection import collect_index, CALENDAR_URL
        def request(url, params):
            if url == CALENDAR_URL:
                return {"data": [{"jyrq": f"2026-10-{day:02d}", "jybz": "1" if day in {23, 26, 27} else "0"} for day in range(1, 32)]}
            provider_symbol = params["param"].split(",")[0]
            return {"code": 0, "data": {provider_symbol: {"day": [[day, 100, close, 104, 99, 10]
                for day in ('2026-10-23', '2026-10-26', '2026-10-27') if start <= day <= end]}}}
        dataset = {"000922": "csi-dividend-price-v1", "000300": "csi300-normalized-v1",
                   "000905": "csi500-price-v1"}[symbol]
        collect_index(root=ROOT, dataset_name=dataset, provider="tencent_raw", start=start, end=end, output=path, request=request)
        if source != "tencent_raw":
            from market_data.release import dataset_identity, _json_bytes
            meta = json.loads((path / "manifest.json").read_text())
            meta["source"] = source
            meta["dataset_id"] = dataset_identity(meta)
            (path / "manifest.json").write_bytes(_json_bytes(meta))

    def _tree(self, root, *, start="2026-10-26", end="2026-10-26", source="tencent_raw", symbol="000922", close=102, missing=False):
        from market_data.history import _write_close_history
        from market_data.cumulative import build_cumulative
        baseline = root / "baseline"
        baseline.mkdir()
        _write_close_history(baseline / "close", [["2026-10-23", 100, 101, 99, 98, 10]], sessions=["2026-10-23", "2026-10-22"] if missing else ["2026-10-23"], symbol="000922", requested_start="2026-10-23", requested_end="2026-10-23", calendar_errata=[])
        first = root / "first"
        build_cumulative(delta_dir=baseline, output_dir=first, as_of="2026-10-23")
        delta = root / "delta"
        delta.mkdir()
        self._price(delta / "price", start=start, end=end, source=source, symbol=symbol, close=close)
        second = root / "second"
        build_cumulative(delta_dir=delta, output_dir=second, as_of=end if end > "2026-10-23" else "2026-10-26", previous_dir=first, previous_release="data-2026-10-23-r1", previous_archive_sha256="a" * 64)
        return second

    def test_same_source_price_extends_close_without_replacing_ohlcv(self):
        from market_data.cumulative import materialize_cumulative, verify_registry_cache
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = materialize_cumulative(self._tree(root), root / "out")
            entries = json.loads(result["registry"].read_text())["datasets"]
            self.assertEqual({item["data_class"] for item in entries}, {"normalized_close", "normalized_price"})
            close = next(item for item in entries if item["data_class"] == "normalized_close")
            store = root / "out" / close["cache_name"]
            meta = json.loads((store / "manifest.json").read_text())
            self.assertEqual(meta["last_date"], "2026-10-26")
            self.assertEqual(meta["row_count"], 2)
            self.assertFalse(meta["ohlcv_validation_passed"])
            self.assertFalse((store / "bars.csv").exists())
            self.assertIn("2026-10-26,102", (store / "close.csv").read_text())
            self.assertEqual(len(meta["close_projections"]), 1)
            verify_registry_cache(root / "out", result["registry"])

    def test_conflicting_projected_close_rejected_atomically(self):
        from market_data.cumulative import materialize_cumulative
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = self._tree(root, start="2026-10-23", end="2026-10-23", close=103)
            with self.assertRaisesRegex(ValueError, "overlapping close"):
                materialize_cumulative(tree, root / "out")
            self.assertFalse((root / "out").exists())

    def test_gap_between_close_and_price_rejected(self):
        from market_data.cumulative import materialize_cumulative
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "gap"):
                materialize_cumulative(self._tree(root, start="2026-10-27", end="2026-10-27"), root / "out")
            self.assertFalse((root / "out").exists())

    def test_projection_does_not_hide_existing_close_gap(self):
        from market_data.cumulative import materialize_cumulative
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "gap"):
                materialize_cumulative(self._tree(root, missing=True), root / "out")

    def test_source_and_symbol_are_exact_projection_boundaries(self):
        from market_data.cumulative import materialize_cumulative
        for options in ({"source": "sina_raw"}, {"symbol": "000300"}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                result = materialize_cumulative(self._tree(root, **options), root / "out")
                entry = next(item for item in json.loads(result["registry"].read_text())["datasets"] if item["data_class"] == "normalized_close")
                meta = json.loads((root / "out" / entry["cache_name"] / "manifest.json").read_text())
                self.assertEqual(meta["last_date"], "2026-10-23")

    def test_verified_calendar_context_bridges_weekday_holidays(self):
        from market_data.cumulative import materialize_cumulative
        from market_data.release import _json_bytes, dataset_identity
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = self._tree(root, start="2026-10-27", end="2026-10-27")
            # Simulate a calendar response explicitly closing Monday 26.
            path = tree / "deltas/2026-10-27/price/manifest.json"
            meta = json.loads(path.read_text())
            meta['calendar_context'] = {'source_id': 'szse_calendar',
                'requested_start': '2026-10-24', 'requested_end': '2026-10-27',
                'expected_sessions': ['2026-10-27']}
            meta['dataset_id'] = dataset_identity(meta)
            path.write_bytes(_json_bytes(meta))
            # Rebuild outer integrity inventory, as a new verified batch would.
            from market_data.cumulative import _files
            outer_path = tree / 'cumulative-manifest.json'
            outer = json.loads(outer_path.read_text())
            outer['files'] = _files(tree)
            outer_path.write_bytes(_json_bytes(outer))
            result = materialize_cumulative(tree, root / 'out')
            entry = next(row for row in json.loads(result['registry'].read_text())['datasets'] if row['data_class'] == 'normalized_close')
            meta = json.loads((root / 'out' / entry['cache_name'] / 'manifest.json').read_text())
            self.assertEqual(meta['last_date'], '2026-10-27')

    def test_collector_records_calendar_context_before_requested_start(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'price'
            self._price(path)
            meta = json.loads((path / 'manifest.json').read_text())
            self.assertEqual(meta['calendar_context']['requested_start'], '2026-10-01')
            self.assertEqual(meta['calendar_context']['expected_sessions'], ['2026-10-23', '2026-10-26'])

    def test_additional_route_recovers_from_its_own_previous_tail(self):
        from market_data.cumulative import build_cumulative, materialize_cumulative
        from market_data.history import backfill
        calls = []
        def unavailable(*args, **kwargs):
            raise ValueError('source is unavailable')
        def collect(**kwargs):
            calls.append(kwargs)
            raise ValueError('source is unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / 'baseline'
            baseline.mkdir()
            self._price(baseline / 'sina', start='2026-10-23', end='2026-10-23', symbol='000300', source='sina_raw')
            build_cumulative(delta_dir=baseline, output_dir=root / 'cumulative', as_of='2026-10-23')
            result = materialize_cumulative(root / 'cumulative', root / 'previous')
            with patch('market_data.history.collect_index', side_effect=collect), patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}):
                backfill(root=ROOT, start='2026-10-27', end='2026-10-27', output=root / 'run',
                    request=unavailable, price_fetch=unavailable, legu_fetch=unavailable,
                    previous_registry=result['registry'])
            recovered = next(call for call in calls if call['dataset_name'] == 'csi300-sina-price-v1')
            self.assertEqual(recovered['start'], '2026-10-24')
            other = next(call for call in calls if call['dataset_name'] == 'csi500-sina-price-v1')
            self.assertEqual(other['start'], '2026-10-27')

    def test_recovered_sina_range_materializes_all_missed_sessions(self):
        from market_data.collection import CALENDAR_URL
        from market_data.cumulative import build_cumulative, materialize_cumulative
        from market_data.history import backfill
        def unavailable(*args, **kwargs):
            raise ValueError('source is unavailable')
        def request(url, params):
            if url == CALENDAR_URL:
                return {'data': [{'jyrq': f'2026-10-{day:02d}',
                    'jybz': '1' if day in {23, 26, 27} else '0'} for day in range(1, 32)]}
            if 'symbol' in params:
                return [{'day': day, 'open': 100, 'close': 102, 'high': 104, 'low': 99, 'volume': 10}
                        for day in ('2026-10-26', '2026-10-27')]
            raise ValueError('source is unavailable')
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}):
            root = Path(temp)
            baseline = root / 'baseline'
            baseline.mkdir()
            self._price(baseline / 'stale', start='2026-10-23', end='2026-10-23', symbol='000300', source='sina_raw')
            self._price(baseline / 'fresh', start='2026-10-26', end='2026-10-26', symbol='000905', source='sina_raw')
            build_cumulative(delta_dir=baseline, output_dir=root / 'first', as_of='2026-10-26')
            previous = materialize_cumulative(root / 'first', root / 'previous')
            report = backfill(root=ROOT, start='2026-10-27', end='2026-10-27', output=root / 'delta',
                previous_registry=previous['registry'], request=request, price_fetch=unavailable, legu_fetch=unavailable)
            prices = {row['symbol']: row['additional_prices'][0] for row in report['indices'] if row['additional_prices']}
            self.assertEqual(prices['000300']['row_count'], 2)
            self.assertEqual(prices['000905']['row_count'], 1)
            build_cumulative(delta_dir=root / 'delta', output_dir=root / 'second', as_of='2026-10-27',
                previous_dir=root / 'first', previous_release='data-2026-10-26-r2', previous_archive_sha256='b' * 64)
            result = materialize_cumulative(root / 'second', root / 'out')
            entry = next(row for row in json.loads(result['registry'].read_text())['datasets'] if row['symbol'] == '000300')
            meta = json.loads((root / 'out' / entry['cache_name'] / 'manifest.json').read_text())
            self.assertEqual(meta['row_count'], 3)
            self.assertEqual(meta['expected_sessions'], ['2026-10-23', '2026-10-26', '2026-10-27'])

    def test_additional_sina_routes_collected_explicitly_and_failures_retained(self):
        from market_data.history import backfill
        calls = []
        def unavailable(*args, **kwargs):
            raise ValueError("synthetic source unavailable")
        def collect(**kwargs):
            calls.append(kwargs)
            if kwargs["dataset_name"] == "csi500-sina-price-v1":
                raise ValueError("source is unavailable")
            self._price(kwargs["output"], symbol="000300")
            return kwargs["output"]
        with tempfile.TemporaryDirectory() as temp, patch("market_data.history.collect_index", side_effect=collect), patch.dict("os.environ", {"GITHUB_ACTIONS": "false"}):
            report = backfill(root=ROOT, start="2026-10-26", end="2026-10-26", output=Path(temp) / "run", request=unavailable, price_fetch=unavailable, legu_fetch=unavailable)
            self.assertEqual(len(calls), 6)
            self.assertTrue(all(call["provider"] == "sina_raw" for call in calls))
            self.assertEqual(len({str(call["output"]) for call in calls}), 6)
            rows = {row["symbol"]: row for row in report["indices"]}
            failed = rows["000905"]["additional_prices"][0]
            self.assertEqual(failed["source"], "sina_raw")
            self.assertEqual(failed["reason_code"], "source_unavailable")
            self.assertEqual(rows["000300"]["additional_prices"][0]["status"], "available")
            self.assertIn("sina_raw", (Path(temp) / "run" / "coverage-report.md").read_text())
