from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from market_data.catalog import build_catalog_report, validate_catalog


ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def _copy_catalog(self, temp: str) -> Path:
        root = Path(temp)
        shutil.copytree(ROOT / "catalog", root / "catalog")
        shutil.copytree(ROOT / "fixtures", root / "fixtures")
        return root

    def test_current_catalog_tracks_selected_indices(self) -> None:
        counts = validate_catalog(ROOT)
        self.assertEqual(counts["tracked"], 13)
        self.assertEqual(counts["datasets"], 27)

    def test_report_contains_no_market_data_and_all_selected_symbols(self) -> None:
        report = build_catalog_report(ROOT)
        self.assertFalse(report["market_data_included"])
        self.assertFalse(report["source_authorization_complete"])
        self.assertEqual(
            [item["symbol"] for item in report["tracked_indices"]],
            [
                "000300",
                "000510",
                "000905",
                "000852",
                "399006",
                "399606",
                "000688",
                "000922",
                "930955",
                "000932",
                "000991",
                "000993",
                "H30269",
            ],
        )

    def test_watchlist_symbol_requires_an_instrument(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._copy_catalog(temp)
            path = root / "catalog" / "watchlist.json"
            watchlist = json.loads(path.read_text(encoding="utf-8"))
            watchlist["symbols"].append("999999")
            path.write_text(json.dumps(watchlist), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "watchlist symbol 999999"):
                validate_catalog(root)

    def test_watchlisted_instrument_requires_each_dataset_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._copy_catalog(temp)
            path = root / "catalog" / "datasets.json"
            datasets = json.loads(path.read_text(encoding="utf-8"))
            datasets = {
                dataset_id: dataset
                for dataset_id, dataset in datasets.items()
                if not (
                    dataset["instrument"] == "csi300"
                    and dataset["data_class"] == "valuation_observation"
                )
            }
            path.write_text(json.dumps(datasets), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "has no valuation_observation dataset"
            ):
                validate_catalog(root)

    def test_catalog_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._copy_catalog(temp)
            path = root / "catalog" / "sources.json"
            raw = path.read_text(encoding="utf-8")
            path.write_text(
                raw.replace(
                    '"tencent_raw": {',
                    '"tencent_raw": {},\n  "tencent_raw": {',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                validate_catalog(root)


if __name__ == "__main__":
    unittest.main()
