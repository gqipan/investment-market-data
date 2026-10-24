from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from market_data.release import (
    assert_release_allowed,
    assert_publication_allowed,
    build_candidate,
    dataset_identity,
    verify_release,
)


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def _build(self, output: Path) -> Path:
        return build_candidate(
            root=ROOT,
            dataset_name="csi300-demo",
            as_of="2026-10-22",
            revision=1,
            output_dir=output,
            generated_at="2026-10-23T00:00:00+00:00",
            code_commit="a" * 40,
        )

    def test_candidate_has_hashes_and_safe_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            release_dir = self._build(Path(temp))
            manifest = json.loads(
                (release_dir / "release-manifest.json").read_text(encoding="utf-8")
            )
            asset = release_dir / manifest["datasets"][0]["asset"]
            self.assertEqual(
                hashlib.sha256(asset.read_bytes()).hexdigest(),
                manifest["datasets"][0]["sha256"],
            )
            self.assertFalse(manifest["private_release_eligible"])
            self.assertFalse(manifest["publication_eligible"])
            verify_release(release_dir)

    def test_synthetic_candidate_cannot_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            release_dir = self._build(Path(temp))
            with self.assertRaisesRegex(ValueError, "release is blocked"):
                assert_release_allowed(ROOT, release_dir, "private")
            with self.assertRaisesRegex(ValueError, "release is blocked"):
                assert_publication_allowed(ROOT, release_dir)

    def test_source_approval_does_not_promote_synthetic_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            shutil.copytree(ROOT / "catalog", root / "catalog")
            shutil.copytree(ROOT / "fixtures", root / "fixtures")
            sources_path = root / "catalog" / "sources.json"
            sources = json.loads(sources_path.read_text(encoding="utf-8"))
            sources["synthetic_fixture"].update(
                {
                    "status": "approved_private",
                    "private_release_allowed": True,
                    "cloud_collection_allowed": True,
                    "cloud_storage_allowed": True,
                }
            )
            sources_path.write_text(json.dumps(sources), encoding="utf-8")
            release_dir = build_candidate(
                root=root,
                dataset_name="csi300-demo",
                as_of="2026-10-22",
                revision=1,
                output_dir=Path(temp) / "out",
                generated_at="2026-10-23T00:00:00+00:00",
                code_commit="a" * 40,
            )
            with self.assertRaisesRegex(ValueError, "synthetic"):
                assert_release_allowed(root, release_dir, "private")
            with self.assertRaisesRegex(ValueError, "release is blocked"):
                assert_release_allowed(root, release_dir, "public")

    def test_candidate_archive_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self._build(root / "first")
            second = self._build(root / "second")
            first_manifest = json.loads(
                (first / "release-manifest.json").read_text(encoding="utf-8")
            )
            second_manifest = json.loads(
                (second / "release-manifest.json").read_text(encoding="utf-8")
            )
            asset = first_manifest["datasets"][0]["asset"]
            self.assertEqual(
                (first / asset).read_bytes(),
                (second / asset).read_bytes(),
            )
            self.assertEqual(
                first_manifest["datasets"][0]["sha256"],
                second_manifest["datasets"][0]["sha256"],
            )

    def test_dataset_identity_matches_existing_reader_algorithm(self) -> None:
        manifest = {"symbol": "000300", "row_count": 1, "published_at": None}
        expected = hashlib.sha256(
            json.dumps(
                manifest,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.assertEqual(dataset_identity(manifest), expected)


if __name__ == "__main__":
    unittest.main()
