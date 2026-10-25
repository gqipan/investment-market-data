import hashlib
import json
from pathlib import Path
import tempfile
import unittest


class CumulativeTests(unittest.TestCase):
    def _batch(self, root: Path, value: str) -> Path:
        root.mkdir()
        snapshot = root / f"price-{value}"
        snapshot.mkdir()
        fixture = Path(__file__).resolve().parents[1] / "fixtures"
        (snapshot / "bars.csv").write_bytes((fixture / "csi300-demo-bars.csv").read_bytes())
        (snapshot / "calendar.csv").write_bytes(
            (fixture / "csi300-demo-calendar.csv").read_bytes()
        )
        from market_data.release import _json_bytes, dataset_identity

        manifest = {
            "schema_version": "normalized-price-local-1",
            "data_class": "normalized_price",
            "synthetic": False,
            "source": "test_source",
            "symbol": "000300",
            "requested_start": "2026-10-16",
            "requested_end": "2026-10-22",
            "expected_sessions": [
                "2026-10-16",
                "2026-10-19",
                "2026-10-20",
                "2026-10-21",
                "2026-10-22",
            ],
            "missing_sessions": [],
            "coverage_verified": True,
            "row_count": 5,
            "first_date": "2026-10-16",
            "last_date": "2026-10-22",
            "sha256": hashlib.sha256((snapshot / "bars.csv").read_bytes()).hexdigest(),
            "calendar_sha256": hashlib.sha256(
                (snapshot / "calendar.csv").read_bytes()
            ).hexdigest(),
            "batch_value": value,
        }
        manifest["dataset_id"] = dataset_identity(manifest)
        (snapshot / "manifest.json").write_bytes(_json_bytes(manifest))
        (root / "request-cache").mkdir()
        (root / "request-cache" / "raw.json").write_text('{"private":true}')
        return root

    def test_baseline_then_delta_preserves_both_batches(self):
        from market_data.cumulative import build_cumulative, verify_cumulative

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._batch(root / "baseline-source", "1")
            first = root / "cumulative-1"
            manifest = build_cumulative(
                delta_dir=baseline,
                output_dir=first,
                as_of="2026-10-23",
            )
            self.assertEqual(manifest["batches"][0]["kind"], "baseline")
            self.assertFalse((first / "baseline" / "request-cache").exists())
            self.assertTrue(
                (first / "baseline" / "price-1" / "bars.csv").is_file()
            )

            delta = self._batch(root / "delta-source", "2")
            second = root / "cumulative-2"
            build_cumulative(
                delta_dir=delta,
                output_dir=second,
                as_of="2026-10-26",
                previous_dir=first,
                previous_release="data-2026-10-23-r1",
                previous_archive_sha256="a" * 64,
            )
            verified = verify_cumulative(second)
            self.assertEqual([row["kind"] for row in verified["batches"]], ["baseline", "delta"])
            self.assertTrue((second / "baseline" / "price-1" / "bars.csv").is_file())
            self.assertTrue(
                (second / "deltas" / "2026-10-26" / "price-2" / "bars.csv").is_file()
            )

    def test_duplicate_cumulative_date_is_rejected(self):
        from market_data.cumulative import build_cumulative

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "cumulative-1"
            build_cumulative(
                delta_dir=self._batch(root / "baseline-source", "1"),
                output_dir=first,
                as_of="2026-10-23",
            )
            with self.assertRaisesRegex(ValueError, "already contains"):
                build_cumulative(
                    delta_dir=self._batch(root / "delta-source", "2"),
                    output_dir=root / "cumulative-2",
                    as_of="2026-10-23",
                    previous_dir=first,
                    previous_release="data-2026-10-23-r1",
                    previous_archive_sha256="b" * 64,
                )

    def test_inventory_detects_changed_previous_file(self):
        from market_data.cumulative import build_cumulative, verify_cumulative

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "cumulative"
            build_cumulative(
                delta_dir=self._batch(root / "source", "1"),
                output_dir=first,
                as_of="2026-10-23",
            )
            (first / "baseline" / "price-1" / "bars.csv").write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_cumulative(first)

    def test_tampered_delta_is_rejected_before_append(self):
        from market_data.cumulative import build_cumulative

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "cumulative-1"
            build_cumulative(
                delta_dir=self._batch(root / "baseline-source", "1"),
                output_dir=first,
                as_of="2026-10-23",
            )
            delta = self._batch(root / "delta-source", "2")
            (delta / "price-2" / "bars.csv").write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                build_cumulative(
                    delta_dir=delta,
                    output_dir=root / "cumulative-2",
                    as_of="2026-10-26",
                    previous_dir=first,
                    previous_release="data-2026-10-23-r1",
                    previous_archive_sha256="c" * 64,
                )
            self.assertFalse((root / "cumulative-2").exists())

    def test_materialize_merges_same_source_batches_for_private_research(self):
        from market_data.cumulative import (
            build_cumulative,
            materialize_cumulative,
            verify_registry_cache,
        )
        from market_data.release import _json_bytes, dataset_identity

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "cumulative-1"
            build_cumulative(
                delta_dir=self._batch(root / "baseline-source", "1"),
                output_dir=first,
                as_of="2026-10-22",
            )
            delta = self._batch(root / "delta-source", "2")
            snapshot = delta / "price-2"
            bars = snapshot / "bars.csv"
            calendar = snapshot / "calendar.csv"
            bars.write_text(
                bars.read_text() + "2026-10-23,101,102,100,101.5,20\n",
                encoding="utf-8",
            )
            calendar.write_text(
                calendar.read_text() + "2026-10-23,true\n",
                encoding="utf-8",
            )
            meta = json.loads((snapshot / "manifest.json").read_text())
            meta.pop("dataset_id")
            meta.update(
                requested_end="2026-10-23",
                expected_sessions=[
                    "2026-10-16",
                    "2026-10-19",
                    "2026-10-20",
                    "2026-10-21",
                    "2026-10-22",
                    "2026-10-23",
                ],
                row_count=6,
                last_date="2026-10-23",
                sha256=hashlib.sha256(bars.read_bytes()).hexdigest(),
                calendar_sha256=hashlib.sha256(calendar.read_bytes()).hexdigest(),
            )
            meta["dataset_id"] = dataset_identity(meta)
            (snapshot / "manifest.json").write_bytes(_json_bytes(meta))

            second = root / "cumulative-2"
            build_cumulative(
                delta_dir=delta,
                output_dir=second,
                as_of="2026-10-23",
                previous_dir=first,
                previous_release="data-2026-10-22-r1",
                previous_archive_sha256="a" * 64,
            )
            result = materialize_cumulative(second, root / "private")
            registry = json.loads(result["registry"].read_text())
            self.assertEqual(result["expected_session"], "2026-10-23")
            self.assertEqual(len(registry["datasets"]), 1)
            entry = registry["datasets"][0]
            self.assertEqual(entry["source"], "test_source")
            materialized = result["registry"].parent / entry["cache_name"]
            from market_data.quality import read_bars

            self.assertEqual(len(read_bars(materialized / "bars.csv")), 6)
            verify_registry_cache(
                result["registry"].parent,
                result["registry"],
            )


if __name__ == "__main__":
    unittest.main()
