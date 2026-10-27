from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import io
import tarfile
from unittest.mock import patch

from market_data.privacy import scan_privacy


class PrivacyTests(unittest.TestCase):
    def test_common_credentials_and_contact_fields_are_rejected_without_echo(self):
        samples = [
            'api_key = "' + 'sk-' + 'x' * 40 + '"',
            'Authorization: ' + 'Bearer ' + 'a' * 40,
            'https://user:' + 'password123@example.invalid/api',
            '{"' + 'phone' + '": "138' + '00000000"}',
            'contact: person' + '@example.dev',
            'api_key: ' + 'opaque' * 6,
            'PASSWORD=' + 'opaque' * 6,
            'Authorization: ' + 'Basic ' + 'YQ==' * 8,
            'Cookie: session=' + 'x' * 40,
        ]
        for sample in samples:
            with self.subTest(kind=samples.index(sample)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'note.md').write_text(sample)
                findings = scan_privacy(root)
                self.assertTrue(findings)
                self.assertNotIn(sample, '\n'.join(findings))

    def test_private_filename_is_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            name = 'person_138' + '00000000.md'
            (root / name).write_text('public metadata')
            findings = scan_privacy(root)
            self.assertTrue(findings)
            self.assertNotIn(name, '\n'.join(findings))

    def test_local_terms_cover_contents_and_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            term = 'synthetic-private-service'
            (root / (term + '.md')).write_text(term.upper())
            findings = scan_privacy(root, private_terms=(term,))
            self.assertTrue(findings)
            self.assertNotIn(term, '\n'.join(findings).lower())

    def test_safe_placeholders_and_public_links_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'note.md').write_text('https://example.org/data user@example.invalid ${API_KEY}')
            self.assertEqual(scan_privacy(root), [])

    def test_hash_like_filename_is_not_a_phone_number(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ('a138' + '00000000b.json')).write_text('{}')
            self.assertEqual(scan_privacy(root), [])

    def test_unreadable_directory_is_not_reported_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('os.scandir', side_effect=PermissionError('synthetic detail')):
                with self.assertRaisesRegex(ValueError, 'traversal'):
                    scan_privacy(Path(temp))

    def test_bom_does_not_hide_private_csv_header(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'data.csv').write_text('\ufeff' + 'account_id' + ',amount\nexample,1\n')
            self.assertTrue(scan_privacy(root))

    def test_archive_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with tarfile.open(root / 'bundle.tar.gz', 'w:gz') as archive:
                info = tarfile.TarInfo('note.md')
                info.uname = 'synthetic-person'
                info.size = 4
                archive.addfile(info, io.BytesIO(b'safe'))
            self.assertTrue(scan_privacy(root))

    def test_large_clean_archive_is_inspected_without_old_member_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with tarfile.open(root / 'bundle.tar.gz', 'w:gz') as archive:
                for i in range(110):
                    info = tarfile.TarInfo(f'{i}.md')
                    info.size = 4
                    archive.addfile(info, io.BytesIO(b'safe'))
            self.assertEqual(scan_privacy(root), [])

    def test_private_home_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "note.md").write_text(
                "generated under /" + "Users/example/private/project\n", encoding="utf-8"
            )
            findings = scan_privacy(root)
            self.assertEqual(len(findings), 1)
            self.assertIn("private path", findings[0])

    def test_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "note.md").write_text("public metadata only\n", encoding="utf-8")
            self.assertEqual(scan_privacy(root), [])


if __name__ == "__main__":
    unittest.main()
