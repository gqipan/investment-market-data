from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from market_data.privacy import scan_privacy


class PrivacyTests(unittest.TestCase):
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
