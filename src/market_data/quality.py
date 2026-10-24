"""Market-data parsing and quality checks with explicit trading sessions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
import math
from pathlib import Path


BAR_FIELDS = ("Date", "Open", "High", "Low", "Close", "Volume")


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def read_bars(path: Path) -> list[Bar]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(BAR_FIELDS):
            raise ValueError(
                f"{path}: expected columns {','.join(BAR_FIELDS)}, got {reader.fieldnames}"
            )
        rows = [row for row in reader]
    if not rows:
        raise ValueError(f"{path}: no bars")

    bars: list[Bar] = []
    for line_no, row in enumerate(rows, start=2):
        try:
            day = date.fromisoformat(row["Date"])
            values = [float(row[field]) for field in BAR_FIELDS[1:]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_no}: invalid bar: {exc}") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"{path}:{line_no}: values must be finite")
        open_, high, low, close, volume = values
        if min(open_, high, low, close) <= 0:
            raise ValueError(f"{path}:{line_no}: OHLC must remain positive")
        if volume < 0:
            raise ValueError(f"{path}:{line_no}: volume must not be negative")
        if high < max(open_, low, close) or low > min(open_, high, close):
            raise ValueError(f"{path}:{line_no}: invalid OHLC relationship")
        bars.append(Bar(day, open_, high, low, close, volume))

    days = [bar.day for bar in bars]
    if len(days) != len(set(days)):
        raise ValueError(f"{path}: duplicate dates")
    if days != sorted(days):
        raise ValueError(f"{path}: dates must be strictly increasing")
    return bars


def read_calendar(path: Path) -> list[date]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["Date", "IsOpen"]:
            raise ValueError(f"{path}: expected columns Date,IsOpen")
        sessions: list[date] = []
        for line_no, row in enumerate(reader, start=2):
            try:
                day = date.fromisoformat(row["Date"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_no}: invalid calendar date") from exc
            if row["IsOpen"].strip().lower() == "true":
                sessions.append(day)
            elif row["IsOpen"].strip().lower() != "false":
                raise ValueError(f"{path}:{line_no}: IsOpen must be true or false")
    if not sessions:
        raise ValueError(f"{path}: calendar has no open sessions")
    if len(sessions) != len(set(sessions)):
        raise ValueError(f"{path}: duplicate calendar dates")
    if sessions != sorted(sessions):
        raise ValueError(f"{path}: calendar dates must be strictly increasing")
    return sessions


def validate_coverage(bars: list[Bar], sessions: list[date]) -> None:
    observed = {bar.day for bar in bars}
    expected = set(sessions)
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    if missing or unexpected:
        raise ValueError(
            "trading-session coverage mismatch: "
            f"missing={','.join(map(str, missing)) or '-'}; "
            f"unexpected={','.join(map(str, unexpected)) or '-'}"
        )
