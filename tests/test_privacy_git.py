from pathlib import Path
import subprocess
import tempfile
import unittest

from market_data import privacy_git


class GitPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, message='safe'):
        self.git('add', '.')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def test_index_is_scanned_instead_of_worktree(self):
        path = self.root / 'README.md'
        path.write_text('Bearer ' + 'a' * 40)
        self.git('add', '.')
        path.write_text('safe')
        self.assertTrue(privacy_git.scan_index(self.root))

    def test_history_checks_intermediate_blobs_and_metadata(self):
        path = self.root / 'README.md'
        path.write_text('safe')
        base = self.commit()
        path.write_text('Bearer ' + 'a' * 40)
        self.commit()
        path.write_text('safe')
        self.commit()
        findings = privacy_git.scan_commits(self.root, base + '..HEAD')
        self.assertTrue(findings)
        self.assertNotIn('a' * 40, '\n'.join(findings))

    def test_commit_identity_and_message_are_checked(self):
        (self.root / 'README.md').write_text('safe')
        base = self.commit()
        self.git('config', 'user.email', 'synthetic-person' + '@example.dev')
        (self.root / 'README.md').write_text('changed')
        self.commit('Bearer ' + 'b' * 40)
        findings = privacy_git.scan_commits(self.root, base + '..HEAD')
        self.assertTrue(any('identity' in finding for finding in findings))
        self.assertTrue(any('credential' in finding for finding in findings))

    def test_public_noreply_and_safe_commit_pass(self):
        (self.root / 'README.md').write_text('safe')
        base = self.commit()
        self.git('config', 'user.email', '123+example' + '@users.noreply.github.com')
        (self.root / 'README.md').write_text('changed')
        self.commit()
        self.assertEqual(privacy_git.scan_commits(self.root, base + '..HEAD'), [])

    def test_noreply_email_still_obeys_local_terms_without_echo(self):
        (self.root / 'README.md').write_text('safe')
        base = self.commit()
        term = 'synthetic-private-service'
        self.git('config', 'user.email', term + '@users.noreply.github.com')
        (self.root / 'README.md').write_text('changed')
        self.commit()
        findings = privacy_git.scan_commits(self.root, base + '..HEAD', private_terms=(term,))
        self.assertTrue(findings)
        self.assertNotIn(term, '\n'.join(findings))

    def test_symlink_and_private_staged_path_are_blocked(self):
        (self.root / 'README.md').symlink_to('missing')
        self.git('add', '.')
        self.assertTrue(privacy_git.scan_index(self.root))

    def test_agent_document_is_allowed(self):
        (self.root / 'AGENTS.md').write_text('public collaboration rules')
        self.git('add', '.')
        self.assertEqual(privacy_git.scan_index(self.root), [])

    def test_worktree_checks_untracked_candidates_but_not_ignored_private_cache(self):
        (self.root / '.gitignore').write_text('data/\n')
        (self.root / 'data').mkdir()
        (self.root / 'data/private.txt').write_text('Bearer ' + 'x' * 40)
        self.assertEqual(privacy_git.scan_worktree(self.root), [])
        (self.root / 'README.md').write_text('Bearer ' + 'x' * 40)
        self.assertTrue(privacy_git.scan_worktree(self.root))

    def test_forced_tracking_cannot_hide_behind_ignore(self):
        (self.root / '.gitignore').write_text('data/\n')
        (self.root / 'data').mkdir()
        (self.root / 'data/private.txt').write_text('safe')
        self.git('add', '-f', 'data/private.txt')
        self.assertTrue(privacy_git.scan_index(self.root))

    def test_terms_file_cannot_be_inside_repository(self):
        path = self.root / 'terms.txt'
        path.write_text('synthetic-private-service')
        with self.assertRaisesRegex(ValueError, 'outside'):
            privacy_git.load_private_terms(path, self.root)

    def test_external_terms_file_is_used_and_missing_file_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'terms.txt'
            path.write_text('# local only\nsynthetic-private-service\n')
            self.assertEqual(privacy_git.load_private_terms(path, self.root), ('synthetic-private-service',))
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'cannot be read'):
                privacy_git.load_private_terms(path, self.root)


if __name__ == '__main__':
    unittest.main()
