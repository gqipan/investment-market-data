"""Resolve the trading sessions that are missing from the latest cumulative release."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import json
from typing import Any, Callable

from .collection import fetch_calendar, request_json


@dataclass(frozen=True)
class ReleaseWindow:
    """A contiguous collection range ending at the newest closed trading session."""

    start: str
    target: str
    sessions: tuple[str, ...]


PRICE_STATUSES = {"available", "close_only", "gapped_close"}


def _parse_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def next_release_window(
    previous_as_of: str,
    upper_bound: str,
    *,
    request: Callable = request_json,
) -> ReleaseWindow | None:
    """Return all verified sessions after the previous release through the upper bound."""
    previous = _parse_day(previous_as_of, "previous release date")
    upper = _parse_day(upper_bound, "upper bound")
    if upper <= previous:
        return None
    sessions = fetch_calendar(
        previous + timedelta(days=1),
        upper,
        request=request,
        allow_empty=True,
    )
    if not sessions:
        return None
    return ReleaseWindow(
        start=sessions[0].isoformat(),
        target=sessions[-1].isoformat(),
        sessions=tuple(day.isoformat() for day in sessions),
    )


def latest_session_on_or_before(
    upper_bound: str,
    *,
    lookback_days: int = 62,
    request: Callable = request_json,
) -> ReleaseWindow:
    """Resolve the newest verified closed session for a bootstrap run."""
    if not isinstance(lookback_days, int) or isinstance(lookback_days, bool) or lookback_days < 1:
        raise ValueError("lookback days must be a positive integer")
    upper = _parse_day(upper_bound, "upper bound")
    sessions = fetch_calendar(
        upper - timedelta(days=lookback_days - 1),
        upper,
        request=request,
        allow_empty=True,
    )
    if not sessions:
        raise ValueError("no verified trading session exists in the bootstrap lookback")
    target = sessions[-1]
    return ReleaseWindow(
        start=target.isoformat(),
        target=target.isoformat(),
        sessions=(target.isoformat(),),
    )


def validate_current_coverage(
    report: dict[str, Any],
    *,
    expected_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """Fail unless every established index reached the requested target session.

    A symbol that was present in the previous cumulative release stays part of
    the contract. Temporary source failures therefore stop publication and are
    retried by the next scheduled run instead of silently advancing the release.
    New unsupported mappings are not imposed retroactively.
    """
    requested_end = report.get("requested_end")
    rows = report.get("indices")
    if not isinstance(requested_end, str) or not isinstance(rows, list):
        raise ValueError("coverage report is missing requested_end or indices")
    _parse_day(requested_end, "requested coverage end")
    expected = set(expected_symbols or set())
    if any(not isinstance(symbol, str) for symbol in expected):
        raise ValueError("expected symbols must be strings")

    by_symbol: dict[str, dict[str, Any]] = {}
    current_symbols: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            raise ValueError("coverage report contains an invalid index row")
        symbol = row["symbol"]
        if symbol in by_symbol:
            raise ValueError(f"coverage report contains duplicate symbol: {symbol}")
        by_symbol[symbol] = row
        price = row.get("price")
        if not isinstance(price, dict):
            continue
        status = price.get("status")
        if status in PRICE_STATUSES and price.get("last_date") == requested_end:
            current_symbols.add(symbol)

    missing = sorted(expected - current_symbols)
    if missing:
        details = []
        for symbol in missing:
            price = (by_symbol.get(symbol) or {}).get("price")
            if not isinstance(price, dict):
                details.append(f"{symbol}=missing-row")
                continue
            status = price.get("status", "unknown")
            last_date = price.get("last_date", "unknown")
            details.append(f"{symbol}={status}:{last_date}")
        raise ValueError(
            "established price series did not reach "
            f"{requested_end}: {', '.join(details)}"
        )
    if not current_symbols:
        raise ValueError("no price history reached requested coverage end")
    return {
        "status": "pass",
        "requested_end": requested_end,
        "expected_symbols": sorted(expected),
        "current_symbols": sorted(current_symbols),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    following = subcommands.add_parser("next")
    following.add_argument("--previous-as-of", required=True)
    following.add_argument("--upper-bound", required=True)
    latest = subcommands.add_parser("latest")
    latest.add_argument("--upper-bound", required=True)
    latest.add_argument("--lookback-days", type=int, default=62)
    args = parser.parse_args(argv)

    window = (
        next_release_window(args.previous_as_of, args.upper_bound)
        if args.command == "next"
        else latest_session_on_or_before(
            args.upper_bound,
            lookback_days=args.lookback_days,
        )
    )
    payload = (
        {"status": "noop"}
        if window is None
        else {"status": "collect", **asdict(window)}
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
