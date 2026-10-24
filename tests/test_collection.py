from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_data import collection
from market_data.release import build_candidate, verify_release

ROOT = Path(__file__).resolve().parents[1]


def calendar_rows(*opens):
    return {'data': [{'jyrq': date(2026, 10, day).isoformat(),
                      'jybz': '1' if date(2026, 10, day).isoformat() in opens else '0'}
                     for day in range(1, 32)]}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        local = patch.dict(os.environ, {"GITHUB_ACTIONS": "false"})
        local.start()
        self.addCleanup(local.stop)

    def test_cloud_collection_is_blocked_before_network(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(ValueError, "cloud collection"):
                    collection.collect_index(root=ROOT, dataset_name='csi300-normalized-v1',
                        start='2026-10-19', end='2026-10-20', output=Path(temp)/'snapshot',
                        request=lambda *_: self.fail('network'))

    def test_unavailable_dividend_low_vol_tencent_mapping_is_blocked_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'not explicitly registered'):
                collection.collect_index(root=ROOT, dataset_name='dividend-low-vol-100-price-v1',
                    start='2026-10-19', end='2026-10-20', output=Path(temp)/'snapshot',
                    request=lambda *_: self.fail('unsupported mapping reached network'))

    def test_calendar_cloud_permissions_are_independent(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'project'
            shutil.copytree(ROOT/'catalog', root/'catalog')
            path=root/'catalog/sources.json'
            sources=json.loads(path.read_text())
            sources['tencent_raw']['cloud_collection_allowed']=True
            path.write_text(json.dumps(sources))
            with patch.dict(os.environ, {'GITHUB_ACTIONS':'true'}):
                with self.assertRaisesRegex(ValueError, 'calendar'):
                    collection.collect_index(root=root,dataset_name='csi300-normalized-v1',
                        start='2026-10-19',end='2026-10-20',output=Path(temp)/'snapshot',
                        request=lambda *_: self.fail('network'))

    def test_collects_real_price_shape_and_keeps_provenance(self):
        calls = []
        def request(url, params):
            calls.append((url, params))
            if 'monthList' in url:
                return calendar_rows('2026-10-19', '2026-10-20')
            return {'code':0,'data':{'sh000300':{'day':[
                ['2026-10-19','100.123','101.234','102.345','99.123','1000'],
                ['2026-10-20','101.234','102.345','103.456','100.123','2000']]}}}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)/'snapshot'
            result = collection.collect_index(root=ROOT, dataset_name='csi300-normalized-v1',
                start='2026-10-19', end='2026-10-20', output=target, request=request)
            manifest = json.loads((target/'manifest.json').read_text())
            self.assertEqual(result, target.resolve())
            self.assertEqual(manifest['source'], 'tencent_raw')
            self.assertEqual(manifest['row_count'], 2)
            self.assertEqual(manifest['calendar_id'], 'szse-month-calendar')
            self.assertIn('100.123', (target/'bars.csv').read_text())
            self.assertFalse(manifest['publication_eligible'])
            self.assertTrue(any('monthList' in url for url,_ in calls))

    def test_collected_snapshot_can_build_verified_candidate(self):
        def request(url, params):
            if 'monthList' in url:
                return calendar_rows('2026-10-19')
            return {'code':0,'data':{'sh000300':{'day':[
                ['2026-10-19','100.123','101.234','102.345','99.123','1000']]}}}
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp)/'snapshot'
            collection.collect_index(root=ROOT,dataset_name='csi300-normalized-v1',
                start='2026-10-19',end='2026-10-19',output=snapshot,request=request)
            release = build_candidate(root=ROOT,dataset_name='csi300-normalized-v1',
                as_of='2026-10-19',revision=1,output_dir=Path(temp)/'out',snapshot_dir=snapshot)
            manifest = verify_release(release)
            self.assertEqual(manifest['source']['id'], 'tencent_raw')
            self.assertEqual(manifest['data_class'], 'normalized_price')
            self.assertFalse(manifest['publication_eligible'])

    def test_calendar_gap_prevents_any_snapshot(self):
        def request(url, params):
            if 'monthList' in url:
                return calendar_rows('2026-10-19', '2026-10-20')
            return {'code':0,'data':{'sh000300':{'day':[
                ['2026-10-19','100','101','102','99','1000']]}}}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)/'snapshot'
            with self.assertRaisesRegex(ValueError, 'coverage'):
                collection.collect_index(root=ROOT, dataset_name='csi300-normalized-v1',
                    start='2026-10-19',end='2026-10-20',output=target,request=request)
            self.assertFalse(target.exists())

    def test_rejects_unknown_dataset_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                collection.collect_index(root=ROOT, dataset_name='unknown', start='2026-10-19',
                    end='2026-10-20',output=Path(temp)/'snapshot',request=lambda *_: self.fail('network'))

    def test_missing_raw_day_does_not_fall_back_to_adjusted(self):
        def request(url, params):
            if 'monthList' in url:
                return calendar_rows('2026-10-19')
            return {'code':0,'data':{'sh000300':{'qfqday':[
                ['2026-10-19','100','101','102','99','1000']]}}}
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'raw'):
                collection.collect_index(root=ROOT,dataset_name='csi300-normalized-v1',
                    start='2026-10-19',end='2026-10-19',output=Path(temp)/'snapshot',request=request)

    def test_refuses_output_inside_tracked_code(self):
        with self.assertRaisesRegex(ValueError, 'output'):
            collection.collect_index(root=ROOT,dataset_name='csi300-normalized-v1',
                start='2026-10-19',end='2026-10-20',output=ROOT/'catalog'/'snapshot',
                request=lambda *_: self.fail('network'))

    def test_refuses_existing_snapshot_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileExistsError):
                collection.collect_index(root=ROOT,dataset_name='csi300-normalized-v1',
                    start='2026-10-19',end='2026-10-20',output=Path(temp),
                    request=lambda *_: self.fail('network'))

    def test_akshare_adapter_preserves_actual_provider(self):
        with patch.object(collection, '_akshare_bars', return_value=([
                {'date':'2026-10-19','open':100,'high':102,'low':99,'close':101,'volume':1000}], 'test-version')):
            with tempfile.TemporaryDirectory() as temp:
                target=Path(temp)/'snapshot'
                collection.collect_index(root=ROOT,dataset_name='csi300-normalized-v1',
                    provider='akshare_eastmoney',start='2026-10-19',end='2026-10-19',output=target,
                    request=lambda *_: calendar_rows('2026-10-19'))
                manifest=json.loads((target/'manifest.json').read_text())
                self.assertEqual(manifest['source'], 'akshare_eastmoney')
                self.assertEqual(manifest['library_version'], 'test-version')
                self.assertIn('eastmoney', manifest['source_url'])

    def test_duplicate_calendar_rows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            collection.fetch_calendar(date(2026,10,19),date(2026,10,20),request=lambda *_:{'data': [
                {'jyrq':'2026-10-19','jybz':'1'},{'jyrq':'2026-10-19','jybz':'1'}]})
