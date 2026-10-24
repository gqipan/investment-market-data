from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from market_data.privacy import scan_privacy
from market_data.release import build_candidate, verify_release, assert_release_allowed

ROOT = Path(__file__).resolve().parents[1]


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        shutil.copytree(ROOT / 'catalog', self.root / 'catalog')
        shutil.copytree(ROOT / 'fixtures', self.root / 'fixtures')
        sources = json.loads((self.root / 'catalog/sources.json').read_text())
        sources['test_provider'] = dict(sources['synthetic_fixture'], status='approved_public',
            private_release_allowed=True, public_release_allowed=True,
            cloud_collection_allowed=True, cloud_storage_allowed=True, redistribution_allowed=True)
        (self.root / 'catalog/sources.json').write_text(json.dumps(sources))
        datasets = json.loads((self.root / 'catalog/datasets.json').read_text())
        datasets['test-price'] = dict(datasets['csi300-demo'], source='test_provider',
            data_class='normalized_price', dataset_key='index.cn.csi.000300.price.test')
        (self.root / 'catalog/datasets.json').write_text(json.dumps(datasets))
        for args in (['init', '-q'], ['add', '.'], ['-c','user.name=Test','-c',
                'user.email=test@example.invalid','commit','-qm','test catalog']):
            subprocess.run(['git','-C',str(self.root),*args], check=True, capture_output=True)
        self.commit = subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'], text=True).strip()

    def build(self, dataset='test-price', code_commit=None):
        return build_candidate(root=self.root, dataset_name=dataset, as_of='2026-10-22',
            revision=1, output_dir=self.base/'out', code_commit=code_commit or self.commit)

    def rewrite(self, release, edit):
        path = release/'release-manifest.json'
        manifest = json.loads(path.read_text())
        edit(manifest)
        path.write_text(json.dumps(manifest))
        self.rehash(release)

    def rehash(self, release):
        path = release/'SHA256SUMS'
        names = [line.split('  ',1)[1] for line in path.read_text().splitlines()]
        path.write_text(''.join(hashlib.sha256((release/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in names))

    def test_valid_private_release_passes(self):
        assert_release_allowed(self.root, self.build(), 'private')

    def test_valid_public_release_passes(self):
        assert_release_allowed(self.root, self.build(), 'public')

    def test_private_only_permission_does_not_grant_public_rights(self):
        path = self.root/'catalog/sources.json'
        sources = json.loads(path.read_text())
        sources['test_provider'].update(status='approved_private',public_release_allowed=False)
        path.write_text(json.dumps(sources))
        with self.assertRaisesRegex(ValueError, 'public release is not approved'):
            assert_release_allowed(self.root, self.build(), 'public')

    def test_nonexistent_full_length_commit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Git provenance'):
            assert_release_allowed(self.root, self.build(code_commit='f'*40), 'public')

    def test_archive_privacy_scan_checks_inner_text(self):
        scan = self.base/'scan'
        scan.mkdir()
        payload = self.base/'payload'
        payload.mkdir()
        (payload/'README.txt').write_text('/'+'Users/example/private')
        with tarfile.open(scan/'bundle.tar.gz','w:gz') as archive:
            archive.add(payload/'README.txt',arcname='README.txt')
        self.assertTrue(scan_privacy(scan))

    def test_catalog_selection_cannot_relabel_another_index(self):
        release = self.build()
        self.rewrite(release, lambda m: m['datasets'][0].update(catalog_dataset='csi500-price-v1'))
        with self.assertRaisesRegex(ValueError, 'catalog|instrument|provider'):
            assert_release_allowed(self.root, release, 'public')

    def test_hidden_payload_cannot_bypass_privacy_check(self):
        release = self.build()
        manifest = json.loads((release/'release-manifest.json').read_text())
        asset = release/manifest['datasets'][0]['asset']
        unpacked = self.base/'unpacked'
        with tarfile.open(asset) as archive:
            archive.extractall(unpacked, filter='data')
        (unpacked/'test-price'/'README.txt').write_text('/'+'Users/example/private')
        with tarfile.open(asset,'w:gz') as archive:
            archive.add(unpacked/'test-price',arcname='.git')
        self.rewrite(release, lambda value: value['datasets'][0].update(
            size=asset.stat().st_size, sha256=hashlib.sha256(asset.read_bytes()).hexdigest()))
        with self.assertRaises(ValueError):
            verify_release(release)

    def test_calendar_release_permissions_are_independent(self):
        path = self.root/'catalog/sources.json'
        sources = json.loads(path.read_text())
        sources['test_calendar'] = dict(sources['test_provider'])
        path.write_text(json.dumps(sources))
        datasets_path = self.root/'catalog/datasets.json'
        datasets = json.loads(datasets_path.read_text())
        datasets['test-price']['calendar_source'] = 'test_calendar'
        datasets_path.write_text(json.dumps(datasets))
        release = self.build()
        sources['test_calendar']['public_release_allowed'] = False
        path.write_text(json.dumps(sources))
        with self.assertRaisesRegex(ValueError, 'calendar'):
            assert_release_allowed(self.root, release, 'public')

    def test_corrupt_archive_is_rejected_by_gate(self):
        release = self.build()
        manifest = json.loads((release/'release-manifest.json').read_text())
        (release/manifest['datasets'][0]['asset']).write_bytes(b'broken')
        with self.assertRaises(ValueError):
            assert_release_allowed(self.root, release, 'public')

    def test_fake_commit_is_rejected(self):
        with self.assertRaises(ValueError):
            assert_release_allowed(self.root, self.build(code_commit='not-a-commit'), 'public')

    def test_dirty_tree_is_rejected(self):
        release = self.build()
        (self.root/'catalog/watchlist.json').write_text('{}')
        with self.assertRaises(ValueError):
            assert_release_allowed(self.root, release, 'public')

    def test_current_public_permission_overrides_old_candidate(self):
        release = self.build()
        path = self.root/'catalog/sources.json'
        sources = json.loads(path.read_text())
        sources['test_provider']['public_release_allowed'] = False
        path.write_text(json.dumps(sources))
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Test','-c',
            'user.email=test@example.invalid','commit','-qm','revoke'],check=True,capture_output=True)
        with self.assertRaisesRegex(ValueError, 'public release is not approved'):
            assert_release_allowed(self.root, release, 'public')

    def test_false_coverage_metadata_is_rejected(self):
        release = self.build()
        self.rewrite(release, lambda manifest: manifest['datasets'][0].update(row_count=999999))
        with self.assertRaisesRegex(ValueError, 'metadata'):
            verify_release(release)

    def test_unlisted_release_file_is_rejected(self):
        release = self.build()
        (release/'private.csv').write_text('account,holdings,cash\nDEMO,1,2\n')
        with self.assertRaises(ValueError):
            verify_release(release)

    def test_missing_manifest_checksum_is_rejected(self):
        release = self.build()
        path = release/'SHA256SUMS'
        path.write_text(''.join(line+'\n' for line in path.read_text().splitlines() if 'release-manifest.json' not in line))
        with self.assertRaises(ValueError):
            verify_release(release)

    def test_unlisted_bundle_file_is_rejected(self):
        release = self.build()
        manifest = json.loads((release/'release-manifest.json').read_text())
        asset = release/manifest['datasets'][0]['asset']
        unpacked = self.base/'unpacked'
        with tarfile.open(asset) as archive:
            archive.extractall(unpacked, filter='data')
        (unpacked/'test-price'/'private.csv').write_text('account,holdings,cash\nDEMO,1,2\n')
        with tarfile.open(asset,'w:gz') as archive:
            archive.add(unpacked/'test-price',arcname='test-price')
        self.rewrite(release, lambda value: value['datasets'][0].update(
            size=asset.stat().st_size, sha256=hashlib.sha256(asset.read_bytes()).hexdigest()))
        with self.assertRaises(ValueError):
            verify_release(release)

    def test_synthetic_cannot_be_promoted_by_permissions(self):
        path = self.root/'catalog/sources.json'
        sources = json.loads(path.read_text())
        sources['synthetic_fixture'].update(sources['test_provider'])
        path.write_text(json.dumps(sources))
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            assert_release_allowed(self.root, self.build('csi300-demo'), 'public')

    def test_generated_private_path_is_scanned(self):
        target = self.base/'scan'/'dist'
        target.mkdir(parents=True)
        (target/'manifest.json').write_text(json.dumps({'path':'/'+'Users/example/private'}))
        self.assertTrue(scan_privacy(target.parent))

    def test_ledger_csv_is_rejected(self):
        target = self.base/'scan'
        target.mkdir()
        (target/'ledger.csv').write_text('account,holdings,cash\nDEMO,1,2\n')
        self.assertTrue(scan_privacy(target))

    def test_database_file_is_rejected(self):
        target = self.base/'scan'
        target.mkdir()
        (target/'accounts.sqlite').write_bytes(b'SQLite format 3\x00')
        self.assertTrue(scan_privacy(target))

    def test_symlink_is_rejected(self):
        target = self.base/'scan'
        target.mkdir()
        (target/'note.md').symlink_to(self.root/'catalog/sources.json')
        self.assertTrue(scan_privacy(target))
