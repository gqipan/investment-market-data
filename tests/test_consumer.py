from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from market_data.consumer import stage_release
from market_data.release import build_candidate, dataset_identity

ROOT = Path(__file__).resolve().parents[1]


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = build_candidate(root=ROOT, dataset_name='csi300-demo',
            as_of='2026-10-22', revision=1, output_dir=self.root/'releases')
        self.raw = (self.release/'release-manifest.json').read_bytes()
        self.pin = hashlib.sha256(self.raw).hexdigest()
        self.dataset_id = json.loads(self.raw)['datasets'][0]['dataset_id']

    def stage(self, **kwargs):
        return stage_release(release_dir=kwargs.pop('release_dir', self.release),
            expected_manifest_sha256=kwargs.pop('pin', self.pin), expected_dataset_id=self.dataset_id,
            cache_dir=self.root/'consumer', **kwargs)

    def test_maps_cn_to_a_and_preserves_upstream_identity(self):
        target = self.stage(allow_synthetic=True)
        manifest = json.loads((target/'manifest.json').read_text())
        self.assertEqual(manifest['market'], 'A')
        self.assertEqual(manifest['upstream_dataset_id'], self.dataset_id)
        self.assertEqual(manifest['upstream_release_manifest_sha256'], self.pin)
        self.assertNotEqual(manifest['dataset_id'], self.dataset_id)
        self.assertEqual(manifest['dataset_id'], dataset_identity(manifest))
        self.assertEqual(hashlib.sha256((target/'bars.csv').read_bytes()).hexdigest(), manifest['sha256'])

    def test_rejects_synthetic_unless_explicitly_testing(self):
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            self.stage()
        self.assertFalse((self.root/'consumer').exists())

    def test_bad_pin_creates_no_cache(self):
        with self.assertRaisesRegex(ValueError, 'manifest.*hash'):
            self.stage(pin='0'*64, allow_synthetic=True)
        self.assertFalse((self.root/'consumer').exists())

    def test_wrong_dataset_id_creates_no_cache(self):
        with self.assertRaisesRegex(ValueError, 'dataset'):
            stage_release(release_dir=self.release,expected_manifest_sha256=self.pin,
                expected_dataset_id='0'*64,cache_dir=self.root/'consumer',allow_synthetic=True)
        self.assertFalse((self.root/'consumer').exists())

    def test_same_version_is_idempotent(self):
        first=self.stage(allow_synthetic=True)
        before=(first/'manifest.json').read_bytes()
        self.assertEqual(first,self.stage(allow_synthetic=True))
        self.assertEqual(before,(first/'manifest.json').read_bytes())

    def test_damaged_cached_version_is_not_overwritten(self):
        first=self.stage(allow_synthetic=True)
        (first/'bars.csv').write_bytes(b'damaged')
        with self.assertRaises(ValueError):
            self.stage(allow_synthetic=True)
        self.assertEqual((first/'bars.csv').read_bytes(),b'damaged')

    def test_self_consistent_poisoned_cache_is_not_trusted(self):
        first=self.stage(allow_synthetic=True)
        manifest_path=first/'manifest.json'
        manifest=json.loads(manifest_path.read_text())
        manifest['symbol']='000905'
        manifest['dataset_id']=dataset_identity(manifest)
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.stage(allow_synthetic=True)

    def test_artifact_download_directory_can_be_normalized(self):
        copied=self.root/'artifact-download'
        shutil.copytree(self.release,copied)
        target=self.stage(release_dir=copied,allow_synthetic=True)
        self.assertTrue((target/'bars.csv').is_file())

    def test_staging_failure_preserves_previous_current_pointer(self):
        cache=self.root/'consumer'
        cache.mkdir()
        (cache/'current.json').write_text('{"version":"previous"}')
        with self.assertRaises(ValueError):
            self.stage(pin='0'*64,allow_synthetic=True)
        self.assertEqual((cache/'current.json').read_text(),'{"version":"previous"}')
