from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from market_data.quality import read_bars, read_calendar, validate_coverage


class QualityTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_duplicate_dates_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "bars.csv",
                "Date,Open,High,Low,Close,Volume\n"
                "2026-10-20,1,2,1,1.5,10\n"
                "2026-10-20,1,2,1,1.5,10\n",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_bars(path)

    def test_invalid_ohlc_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "bars.csv",
                "Date,Open,High,Low,Close,Volume\n2026-10-20,1,0.5,0.4,1.5,10\n",
            )
            with self.assertRaisesRegex(ValueError, "OHLC"):
                read_bars(path)

    def test_coverage_requires_exact_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bars = read_bars(
                self._write(
                    root,
                    "bars.csv",
                    "Date,Open,High,Low,Close,Volume\n2026-10-20,1,2,1,1.5,10\n",
                )
            )
            sessions = read_calendar(
                self._write(
                    root,
                    "calendar.csv",
                    "Date,IsOpen\n2026-10-19,true\n2026-10-20,true\n",
                )
            )
            with self.assertRaisesRegex(ValueError, "missing="):
                validate_coverage(bars, sessions)


if __name__ == "__main__":
    unittest.main()
