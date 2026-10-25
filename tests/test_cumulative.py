import json
from pathlib import Path
import tempfile
import unittest


class CumulativeTests(unittest.TestCase):
    def _batch(self, root: Path, value: str) -> Path:
        root.mkdir()
        (root / "observations.csv").write_text(f"Date,Value\n2026-10-23,{value}\n")
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
            self.assertTrue((second / "baseline" / "observations.csv").is_file())
            self.assertTrue((second / "deltas" / "2026-10-26" / "observations.csv").is_file())

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
            (first / "baseline" / "observations.csv").write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_cumulative(first)


if __name__ == "__main__":
    unittest.main()
