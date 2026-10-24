from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from market_data.general_release import (
    assert_snapshot_release_allowed,
    build_snapshot_candidate,
    stage_snapshot_release,
    verify_snapshot_release,
)
from market_data.history import _write_close_history
from market_data.transport import fetch_snapshot_release
from market_data.valuation import write_valuation


ROOT = Path(__file__).resolve().parents[1]


class GeneralReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        shutil.copytree(ROOT / "catalog", self.root / "catalog")
        shutil.copytree(ROOT / "fixtures", self.root / "fixtures")
        sources_path = self.root / "catalog" / "sources.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        for source_id in ("tencent_raw", "szse_calendar", "akshare_legulegu"):
            sources[source_id].update(
                status="approved_public",
                private_release_allowed=True,
                public_release_allowed=True,
                cloud_collection_allowed=True,
                cloud_storage_allowed=True,
                redistribution_allowed=True,
            )
        sources_path.write_text(json.dumps(sources), encoding="utf-8")
        for args in (
            ("init", "-q"),
            ("add", "."),
            (
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "test catalog",
            ),
        ):
            subprocess.run(
                ("git", "-C", str(self.root), *args),
                check=True,
                capture_output=True,
            )
        self.commit = subprocess.check_output(
            ("git", "-C", str(self.root), "rev-parse", "HEAD"),
            text=True,
        ).strip()

    def _close_snapshot(self) -> Path:
        snapshot = self.base / "close"
        _write_close_history(
            snapshot,
            [
                ["2026-10-22", 100, 101, 102, 99, 1000],
                ["2026-10-23", 101, 102, 103, 100, 1000],
            ],
            sessions=["2026-10-22", "2026-10-23"],
            symbol="000300",
            requested_start="2026-10-22",
            requested_end="2026-10-23",
            calendar_errata=[],
        )
        return snapshot

    def _valuation_snapshot(self) -> Path:
        snapshot = self.base / "valuation"
        write_valuation(
            snapshot,
            [
                {
                    "Date": "2026-10-22",
                    "Metric": "pe_ttm",
                    "Value": 12.5,
                    "Unit": "multiple",
                    "Method": "aggregate_ttm",
                },
                {
                    "Date": "2026-10-23",
                    "Metric": "pe_ttm",
                    "Value": 12.8,
                    "Unit": "multiple",
                    "Method": "aggregate_ttm",
                },
            ],
            symbol="000300",
            source="akshare_legulegu",
            raw={},
            requested_start="2026-10-22",
            requested_end="2026-10-23",
        )
        return snapshot

    def _build(self, snapshot: Path, dataset: str, output: str) -> Path:
        return build_snapshot_candidate(
            root=self.root,
            dataset_name=dataset,
            snapshot_dir=snapshot,
            as_of="2026-10-23",
            revision=1,
            output_dir=self.base / output,
            generated_at="2026-10-24T00:00:00+00:00",
            code_commit=self.commit,
        )

    def _pins(self, release: Path) -> tuple[str, str]:
        raw = (release / "release-manifest.json").read_bytes()
        manifest = json.loads(raw)
        return hashlib.sha256(raw).hexdigest(), manifest["datasets"][0]["dataset_id"]

    def test_close_release_builds_verifies_and_stages_without_pointer(self) -> None:
        release = self._build(
            self._close_snapshot(), "csi300-normalized-v1", "close-releases"
        )
        manifest = verify_snapshot_release(release)
        self.assertEqual(manifest["data_class"], "normalized_close")
        self.assertTrue(manifest["publication_eligible"])
        assert_snapshot_release_allowed(self.root, release, "public")
        pin, dataset_id = self._pins(release)
        cache = self.base / "consumer"
        target = stage_snapshot_release(
            release_dir=release,
            expected_manifest_sha256=pin,
            expected_dataset_id=dataset_id,
            expected_data_class="normalized_close",
            cache_dir=cache,
        )
        self.assertEqual(target.name, pin)
        self.assertEqual(
            {path.name for path in target.iterdir()},
            {"close.csv", "raw-bars.json", "manifest.json"},
        )
        self.assertFalse((cache / "current.json").exists())

    def test_valuation_release_preserves_metric_frequency(self) -> None:
        release = self._build(
            self._valuation_snapshot(), "csi300-valuation-v1", "valuation-releases"
        )
        manifest = verify_snapshot_release(release)
        quality = json.loads((release / "quality-report.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["data_class"], "valuation_observation")
        self.assertEqual(
            quality["checks"]["metrics"]["pe_ttm"]["method"], "aggregate_ttm"
        )
        assert_snapshot_release_allowed(self.root, release, "private")

    def test_private_cloud_permissions_do_not_grant_public_release(self) -> None:
        sources_path = self.root / "catalog" / "sources.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        for source_id in ("tencent_raw", "szse_calendar"):
            sources[source_id].update(
                status="approved_private",
                private_release_allowed=True,
                public_release_allowed=False,
                private_cloud_collection_allowed=True,
                private_cloud_storage_allowed=True,
                cloud_collection_allowed=False,
                cloud_storage_allowed=False,
                redistribution_allowed=False,
            )
        sources_path.write_text(json.dumps(sources), encoding="utf-8")
        subprocess.run(
            ("git", "-C", str(self.root), "add", "catalog/sources.json"),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git", "-C", str(self.root),
                "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid",
                "commit", "-qm", "private cloud policy",
            ),
            check=True,
            capture_output=True,
        )
        self.commit = subprocess.check_output(
            ("git", "-C", str(self.root), "rev-parse", "HEAD"),
            text=True,
        ).strip()

        release = self._build(
            self._close_snapshot(), "csi300-normalized-v1", "private-cloud-releases"
        )
        manifest = verify_snapshot_release(release)
        self.assertTrue(manifest["private_release_eligible"])
        self.assertFalse(manifest["publication_eligible"])
        assert_snapshot_release_allowed(self.root, release, "private")
        with self.assertRaisesRegex(ValueError, "public release is not approved"):
            assert_snapshot_release_allowed(self.root, release, "public")

    def test_fixed_remote_release_is_downloaded_and_staged(self) -> None:
        release = self._build(
            self._close_snapshot(), "csi300-normalized-v1", "remote-releases"
        )
        pin, dataset_id = self._pins(release)
        base = (
            "https://github.com/example/market/releases/download/"
            f"{release.name}/"
        )
        metadata = {
            "tag_name": release.name,
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "assets": [
                {
                    "name": path.name,
                    "browser_download_url": base + path.name,
                    "size": path.stat().st_size,
                }
                for path in release.iterdir()
            ],
        }

        def download(url: str, limit: int) -> bytes:
            if url.startswith("https://api.github.com/"):
                return json.dumps(metadata).encode()
            return (release / url.rsplit("/", 1)[-1]).read_bytes()

        target = fetch_snapshot_release(
            repository="example/market",
            release_id=release.name,
            expected_manifest_sha256=pin,
            expected_dataset_id=dataset_id,
            expected_data_class="normalized_close",
            cache_dir=self.base / "remote-cache",
            download=download,
        )
        self.assertEqual(target.name, pin)
        self.assertEqual(target, fetch_snapshot_release(
            repository="example/market",
            release_id=release.name,
            expected_manifest_sha256=pin,
            expected_dataset_id=dataset_id,
            expected_data_class="normalized_close",
            cache_dir=self.base / "remote-cache",
            download=download,
        ))

    def test_pin_class_and_non_public_gates_fail_closed(self) -> None:
        release = self._build(
            self._close_snapshot(), "csi300-normalized-v1", "gate-releases"
        )
        pin, dataset_id = self._pins(release)
        with self.assertRaisesRegex(ValueError, "manifest hash"):
            stage_snapshot_release(
                release_dir=release,
                expected_manifest_sha256="0" * 64,
                expected_dataset_id=dataset_id,
                expected_data_class="normalized_close",
                cache_dir=self.base / "bad-pin",
            )
        with self.assertRaisesRegex(ValueError, "data class"):
            stage_snapshot_release(
                release_dir=release,
                expected_manifest_sha256=pin,
                expected_dataset_id=dataset_id,
                expected_data_class="valuation_observation",
                cache_dir=self.base / "wrong-class",
            )
        self.assertFalse((self.base / "bad-pin").exists())
        self.assertFalse((self.base / "wrong-class").exists())

        path = release / "release-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["publication_eligible"] = False
        path.write_text(json.dumps(manifest), encoding="utf-8")
        names = [line.split("  ", 1)[1] for line in (release / "SHA256SUMS").read_text().splitlines()]
        (release / "SHA256SUMS").write_text(
            "".join(
                f"{hashlib.sha256((release / name).read_bytes()).hexdigest()}  {name}\n"
                for name in names
            ),
            encoding="utf-8",
        )
        raw = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "not approved"):
            stage_snapshot_release(
                release_dir=release,
                expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
                expected_dataset_id=dataset_id,
                expected_data_class="normalized_close",
                cache_dir=self.base / "non-public",
            )


if __name__ == "__main__":
    unittest.main()
