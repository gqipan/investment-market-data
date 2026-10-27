import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from market_data import publication


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.delta = self.root / 'delta'
        self.cumulative = self.root / 'cumulative'
        self.delta.mkdir()
        self.cumulative.mkdir()
        (self.delta / 'coverage-report.json').write_text('{"status":"partial"}')
        (self.delta / 'coverage-report.md').write_text('public coverage')
        (self.delta / 'bars.csv').write_text('Date,Close\n2026-10-26,1\n')
        (self.delta / 'request-cache').mkdir()
        (self.delta / 'request-cache' / 'private.bin').write_bytes(b'private request cache')
        (self.cumulative / 'bars.csv').write_bytes((self.delta / 'bars.csv').read_bytes())
        data = (self.cumulative / 'bars.csv').read_bytes()
        (self.cumulative / 'cumulative-manifest.json').write_text(json.dumps({
            'schema_version': 'cumulative-market-data-1', 'as_of': '2026-10-26',
            'files': [{'path': 'bars.csv', 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}],
        }))

    def prepare(self):
        return publication.prepare_publication(delta_dir=self.delta, cumulative_dir=self.cumulative,
                                               as_of='2026-10-26', output_dir=self.root / 'upload')

    def test_clean_payload_is_exact_and_archive_identities_are_empty(self):
        output = self.prepare()
        self.assertEqual({p.name for p in output.iterdir()}, {
            'index-data-cumulative-2026-10-26.tar.gz', 'index-data-delta-2026-10-26.tar.gz',
            'SHA256SUMS', 'coverage-report.json', 'coverage-report.md',
        })
        for path in output.glob('*.tar.gz'):
            with tarfile.open(path) as archive:
                for member in archive:
                    self.assertEqual((member.uid, member.gid, member.uname, member.gname, member.mtime), (0, 0, '', '', 0))
                    self.assertNotIn('request-cache', member.name)

    def test_private_standalone_report_blocks_atomic_output(self):
        (self.delta / 'coverage-report.md').write_text('Authorization: ' + 'Bearer ' + 'x' * 40)
        with self.assertRaisesRegex(ValueError, 'privacy'):
            self.prepare()
        self.assertFalse((self.root / 'upload').exists())

    def test_corrupt_manifest_error_does_not_echo_unsafe_path(self):
        path = self.cumulative / 'cumulative-manifest.json'
        manifest = json.loads(path.read_text())
        private_name = 'person_138' + '00000000.csv'
        (self.cumulative / 'bars.csv').rename(self.cumulative / private_name)
        manifest['files'][0]['path'] = private_name
        manifest['files'][0]['sha256'] = 'a' * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError) as caught:
            self.prepare()
        self.assertNotIn(private_name, str(caught.exception))

    def test_unknown_text_file_blocks_output(self):
        (self.delta / 'personal-note.md').write_text('apparently harmless')
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            self.prepare()
        self.assertFalse((self.root / 'upload').exists())

    def test_public_registry_is_allowed_and_private_fields_are_rejected(self):
        registry = self.delta / 'registry.json'
        registry.write_text('{"datasets": []}')
        self.assertTrue(self.prepare().is_dir())
        registry.write_text('{"holdings": [1]}')
        with self.assertRaisesRegex(ValueError, 'privacy'):
            publication.prepare_publication(delta_dir=self.delta, cumulative_dir=self.cumulative,
                as_of='2026-10-26', output_dir=self.root / 'second-upload')

    def test_cumulative_addition_report_is_allowed_and_scanned(self):
        report = self.cumulative / 'h30269-addition-report.json'
        report.write_text('{"status":"partial"}')
        manifest_path = self.cumulative / 'cumulative-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        data = report.read_bytes()
        manifest['files'].append({'path': report.name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
        manifest_path.write_text(json.dumps(manifest))
        self.assertTrue(self.prepare().is_dir())
        report.write_text('{"holdings":[1]}')
        data = report.read_bytes()
        manifest['files'][-1].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'privacy'):
            publication.prepare_publication(delta_dir=self.delta, cumulative_dir=self.cumulative,
                as_of='2026-10-26', output_dir=self.root / 'second-upload')

    def test_symlink_is_rejected_before_packaging(self):
        (self.delta / 'linked').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.prepare()

    def test_local_terms_block_release(self):
        (self.delta / 'coverage-report.md').write_text('synthetic-private-service')
        with self.assertRaisesRegex(ValueError, 'privacy'):
            publication.prepare_publication(delta_dir=self.delta, cumulative_dir=self.cumulative,
                as_of='2026-10-26', output_dir=self.root / 'upload',
                private_terms=('synthetic-private-service',))

    def test_real_builder_archive_roundtrip_retains_cumulative_contract(self):
        from test_cumulative import CumulativeTests
        from market_data.cumulative import build_cumulative, extract_cumulative_archive, verify_cumulative
        delta = CumulativeTests()._batch(self.root / 'built-delta', '1')
        (delta / 'coverage-report.json').write_text('{"status":"partial"}')
        (delta / 'coverage-report.md').write_text('public coverage')
        cumulative = self.root / 'built-cumulative'
        build_cumulative(delta_dir=delta, output_dir=cumulative, as_of='2026-10-22')
        output = publication.prepare_publication(delta_dir=delta, cumulative_dir=cumulative,
            as_of='2026-10-22', output_dir=self.root / 'built-upload')
        extracted = self.root / 'extracted'
        extract_cumulative_archive(output / 'index-data-cumulative-2026-10-22.tar.gz', extracted)
        self.assertEqual(verify_cumulative(extracted), verify_cumulative(cumulative))

    def test_workflow_uploads_only_after_preparation(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/daily-release.yml').read_text()
        self.assertLess(workflow.index('prepare-publication'), workflow.index('gh release create'))
        self.assertNotIn('tar -czf', workflow)
        self.assertNotIn('"${RUN}/coverage-report.json" \\', workflow)
        self.assertNotIn('cat "${RUN}/coverage-report.md"', workflow)


if __name__ == '__main__':
    unittest.main()
