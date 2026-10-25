"""Resolve the trading sessions that are missing from the latest cumulative release."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import json
from typing import Callable

from .collection import fetch_calendar, request_json


@dataclass(frozen=True)
class ReleaseWindow:
    """A contiguous collection range ending at the newest closed trading session."""

    start: str
    target: str
    sessions: tuple[str, ...]


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
