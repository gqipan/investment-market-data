"""Same-session immutable source repairs and complete calendar evidence."""
import calendar
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import test_continuity
ROOT = test_continuity.ROOT


class RevisionTests(unittest.TestCase):
    def test_january_and_march_context_cannot_claim_unobserved_february(self):
        from market_data.collection import collect_index, CALENDAR_URL
        from market_data.cumulative import _merge_price_snapshots
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshots = []
            for day in ('2026-01-30', '2026-03-02'):
                def request(url, params):
                    if url == CALENDAR_URL:
                        year, month = map(int, params['month'].split('-'))
                        return {'data': [{'jyrq': date(year, month, number).isoformat(),
                            'jybz': '1' if date(year, month, number).isoformat() == day else '0'}
                            for number in range(1, calendar.monthrange(year, month)[1] + 1)]}
                    symbol = params['param'].split(',')[0]
                    return {'code': 0, 'data': {symbol: {'day': [[day, 100, 101, 102, 99, 10]]}}}
                path = root / day
                collect_index(root=ROOT, dataset_name='csi300-normalized-v1', start=day, end=day, output=path, request=request)
                snapshots.append((day, path, json.loads((path / 'manifest.json').read_text())))
            with self.assertRaisesRegex(ValueError, 'calendar.*gap'):
                _merge_price_snapshots(snapshots, root / 'merged')

    def test_overlapping_calendar_context_cannot_change_closed_to_open(self):
        from market_data.cumulative import _merge_price_snapshots
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = test_continuity.ContinuityTests()
            snapshots = []
            for day in ('2026-10-23', '2026-10-26'):
                path = root / day
                helper._price(path, start=day, end=day, symbol='000300')
                meta = json.loads((path / 'manifest.json').read_text())
                if day.endswith('26'):
                    meta['calendar_context']['expected_sessions'] = ['2026-10-26']
                    from market_data.release import dataset_identity, _json_bytes
                    meta['dataset_id'] = dataset_identity(meta)
                    (path / 'manifest.json').write_bytes(_json_bytes(meta))
                snapshots.append((day, path, meta))
            with self.assertRaisesRegex(ValueError, 'calendar.*conflict'):
                _merge_price_snapshots(snapshots, root / 'merged')

    def _case(self, root):
        from market_data.cumulative import build_cumulative, materialize_cumulative
        helper = test_continuity.ContinuityTests()
        baseline = root / 'baseline'
        baseline.mkdir()
        helper._price(baseline / 'sina', start='2026-10-23', end='2026-10-23', symbol='000300', source='sina_raw')
        build_cumulative(delta_dir=baseline, output_dir=root / 'first', as_of='2026-10-23')
        delta = root / 'delta'
        delta.mkdir()
        helper._price(delta / 'tencent', symbol='000300')
        previous = root / 'previous'
        build_cumulative(delta_dir=delta, output_dir=previous, as_of='2026-10-26', previous_dir=root / 'first',
            previous_release='data-2026-10-23-r1', previous_archive_sha256='a' * 64)
        registry = materialize_cumulative(previous, root / 'materialized')['registry']
        catalog_root = root / 'repo'
        shutil.copytree(ROOT / 'catalog', catalog_root / 'catalog')
        watch = catalog_root / 'catalog/watchlist.json'
        data = json.loads(watch.read_text())
        data['additional_price_datasets'] = ['csi300-sina-price-v1']
        watch.write_text(json.dumps(data))
        return previous, registry, catalog_root

    def test_failed_sina_recovers_same_day_then_second_run_is_noop(self):
        from market_data.history import repair_additional_prices
        from market_data.cumulative import revise_cumulative, materialize_cumulative, verify_cumulative
        from market_data.collection import CALENDAR_URL
        def unavailable(*args, **kwargs):
            raise ValueError('source is unavailable')
        def available(url, params):
            if url == CALENDAR_URL:
                return {'data': [{'jyrq': f'2026-10-{day:02d}', 'jybz': '1' if day in {23, 26} else '0'} for day in range(1, 32)]}
            return [{'day': '2026-10-26', 'open': 100, 'high': 104, 'low': 99, 'close': 102, 'volume': 10}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous, registry, catalog_root = self._case(root)
            old_files = {path.relative_to(previous): path.read_bytes() for path in previous.rglob('*') if path.is_file()}
            failed = repair_additional_prices(root=catalog_root, previous_registry=registry, as_of='2026-10-26', output=root / 'failed', request=unavailable)
            self.assertEqual(failed['added'], 0)
            self.assertEqual(failed['indices'][0]['additional_prices'][0]['reason_code'], 'source_unavailable')
            recovered = repair_additional_prices(root=catalog_root, previous_registry=registry, as_of='2026-10-26', output=root / 'repair', request=available)
            self.assertEqual(recovered['added'], 1)
            revised = root / 'revised'
            manifest = revise_cumulative(previous_dir=previous, repair_dir=root / 'repair', output_dir=revised,
                as_of='2026-10-26', previous_release='data-2026-10-26-r2', previous_archive_sha256='b' * 64)
            self.assertEqual(manifest['previous_archive_sha256'], 'b' * 64)
            self.assertEqual(manifest['previous_release'], 'data-2026-10-26-r2')
            for relative, raw in old_files.items():
                if relative.name != 'cumulative-manifest.json':
                    self.assertEqual((revised / relative).read_bytes(), raw)
                self.assertEqual((previous / relative).read_bytes(), raw)
            self.assertEqual(verify_cumulative(revised)['as_of'], '2026-10-26')
            new_registry = materialize_cumulative(revised, root / 'new-materialized')['registry']
            noop = repair_additional_prices(root=catalog_root, previous_registry=new_registry, as_of='2026-10-26', output=root / 'noop', request=unavailable)
            self.assertEqual(noop['status'], 'noop')
            self.assertEqual(noop['added'], 0)
            with self.assertRaisesRegex(ValueError, 'duplicate route'):
                revise_cumulative(previous_dir=revised, repair_dir=root / 'repair', output_dir=root / 'conflict',
                    as_of='2026-10-26', previous_release='data-2026-10-26-r3', previous_archive_sha256='c' * 64)
            self.assertFalse((root / 'conflict').exists())

    def test_revision_refuses_unregistered_sina_route(self):
        from market_data.cumulative import revise_cumulative
        from market_data.release import dataset_identity, _json_bytes
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous, registry, catalog_root = self._case(root)
            repair = root / 'repair'
            repair.mkdir()
            path = repair / 'unregistered'
            test_continuity.ContinuityTests()._price(path, symbol='000300', source='sina_raw')
            meta = json.loads((path / 'manifest.json').read_text())
            meta['symbol'] = 'H30269'
            meta['dataset_id'] = dataset_identity(meta)
            (path / 'manifest.json').write_bytes(_json_bytes(meta))
            with self.assertRaisesRegex(ValueError, 'registered'):
                revise_cumulative(previous_dir=previous, repair_dir=repair, output_dir=root / 'out',
                    as_of='2026-10-26', previous_release='data-2026-10-26-r2', previous_archive_sha256='d' * 64)
            self.assertFalse((root / 'out').exists())

    def test_revision_refuses_wrong_session_empty_repair_or_unpinned_predecessor(self):
        from market_data.cumulative import revise_cumulative
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous, registry, catalog_root = self._case(root)
            empty = root / 'empty'
            empty.mkdir()
            for options in ({'as_of': '2026-10-27'}, {'previous_archive_sha256': 'invalid'}, {}):
                args = dict(previous_dir=previous, repair_dir=empty, output_dir=root / 'out', as_of='2026-10-26',
                    previous_release='data-2026-10-26-r2', previous_archive_sha256='d' * 64)
                args.update(options)
                with self.assertRaises(ValueError):
                    revise_cumulative(**args)
                self.assertFalse((root / 'out').exists())
