"""Local index snapshots: explicit providers, independent calendar, atomic writes."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import os

from .privacy import scan_privacy
from .quality import Bar, read_bars, read_calendar, validate_coverage
from .release import _json_bytes, _write_bars, _write_calendar, dataset_identity


TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
CALENDAR_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
PROVIDERS = {"tencent_raw", "akshare_eastmoney"}


def request_json(url: str, params: dict):
    request = Request(url + "?" + urlencode(params), headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/" if "szse.cn" in url else "https://gu.qq.com/",
    })
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError("source response exceeds 10 MiB")
            return json.loads(raw)
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                raise ValueError("public source is unavailable after three attempts") from exc
            time.sleep(attempt + 1)


def fetch_calendar(start: date, end: date, *, request=request_json) -> list[date]:
    sessions = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        payload = request(CALENDAR_URL, {"month": f"{year}-{month:02d}"})
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError("independent calendar is unavailable")
        observed = set()
        for row in rows:
            day = date.fromisoformat(str(row["jyrq"])[:10])
            if day in observed:
                raise ValueError("duplicate calendar date")
            observed.add(day)
            if str(row.get("jybz")) not in {"0", "1"}:
                raise ValueError("invalid calendar open flag")
            if start <= day <= end and str(row["jybz"]) == "1":
                sessions.append(day)
        expected = {date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1)}
        if observed != expected:
            raise ValueError("independent calendar month is incomplete")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if not sessions:
        raise ValueError("requested range has no verified trading sessions")
    return sorted(sessions)


def _tencent_bars(symbol: str, start: date, end: date, request) -> list[Bar]:
    bars = []
    for year in range(start.year, end.year + 1):
        first, last = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        payload = request(TENCENT_URL, {"param": f"{symbol},day,{first},{last},640,"})
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise ValueError("Tencent raw index response failed")
        rows = payload.get("data", {}).get(symbol, {}).get("day")
        if not isinstance(rows, list) or not rows:
            raise ValueError("Tencent raw day series is missing; adjusted data is not accepted")
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise ValueError("Tencent raw day fields are incomplete")
            day = date.fromisoformat(row[0])
            if not first <= day <= last:
                raise ValueError("Tencent returned a date outside the requested range")
            open_, close, high, low, volume = map(float, row[1:6])
            bars.append(Bar(day, open_, high, low, close, volume))
    return bars


def _akshare_bars(symbol: str, start: date, end: date) -> tuple[list[dict], str]:
    # Bound the whole library call: supported AkShare versions do not expose HTTP timeouts.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "market_data.akshare_worker", symbol,
             start.strftime("%Y%m%d"), end.strftime("%Y%m%d")],
            capture_output=True, text=True, check=True, timeout=120,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ValueError("AkShare collection failed or timed out; install the collection extra and check connectivity") from exc
    return payload["bars"], payload["version"]


def collect_index(*, root: Path, dataset_name: str, start: str, end: str,
                  output: Path, provider: str | None = None, request=request_json) -> Path:
    root, output = root.resolve(), output.resolve()
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("start date must not follow end date")
    if output.exists():
        raise FileExistsError("snapshot already exists")
    if output.is_relative_to(root) and not output.is_relative_to(root / "data"):
        raise ValueError("collection output must be outside the repository or under ignored data/")
    datasets = json.loads((root / "catalog/datasets.json").read_text(encoding="utf-8"))
    dataset = datasets.get(dataset_name)
    if not dataset or dataset.get("data_class") != "normalized_price":
        raise ValueError("unknown or non-price dataset")
    instruments = json.loads((root / "catalog/instruments.json").read_text(encoding="utf-8"))
    instrument = instruments[dataset["instrument"]]
    provider = provider or dataset["source"]
    if provider not in PROVIDERS:
        raise ValueError("dataset has no configured collection provider")
    sources = json.loads((root / "catalog/sources.json").read_text(encoding="utf-8"))
    source = sources[provider]
    calendar_source = sources.get("szse_calendar", {})
    if source.get("status") == "restricted" or source.get("local_collection_allowed") is not True:
        raise ValueError("local research collection is not enabled for this source")
    if os.environ.get("GITHUB_ACTIONS") == "true" and source.get("cloud_collection_allowed") is not True:
        raise ValueError("cloud collection is not approved")
    if calendar_source.get("local_collection_allowed") is not True:
        raise ValueError("local calendar collection is not enabled")
    if os.environ.get("GITHUB_ACTIONS") == "true" and calendar_source.get("cloud_collection_allowed") is not True:
        raise ValueError("cloud calendar collection is not approved")
    if instrument.get("asset_type") != "index" or instrument.get("series_type") != "price_index":
        raise ValueError("collector accepts registered price indices only")
    symbol = instrument.get("provider_symbols", {}).get(provider)
    if not symbol:
        raise ValueError("index provider symbol is not explicitly registered")
    sessions = fetch_calendar(first, last, request=request)
    library_version = None
    if provider == "tencent_raw":
        bars = _tencent_bars(symbol, first, last, request)
        source_url = TENCENT_URL
    else:
        records, library_version = _akshare_bars(symbol, first, last)
        bars = [Bar(date.fromisoformat(str(row["date"])[:10]), *[
            float(row[key]) for key in ("open", "high", "low", "close", "volume")]) for row in records]
        source_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".collect-", dir=output.parent) as temp:
        staging = Path(temp)
        _write_bars(staging / "bars.csv", bars)
        _write_calendar(staging / "calendar.csv", sessions)
        bars = read_bars(staging / "bars.csv")
        validate_coverage(bars, read_calendar(staging / "calendar.csv"))
        metadata = {
            "source": provider, "source_url": source_url, "source_symbol": symbol,
            "symbol": instrument["symbol"], "dataset_name": dataset_name,
            "dataset_key": f"{instrument['canonical_id']}.{provider}",
            "data_class": "normalized_price", "synthetic": False,
            "market": instrument["market"], "currency": instrument["currency"],
            "series_type": instrument["series_type"], "timezone": instrument["timezone"],
            "frequency": "1d", "adjust": "raw", "adapter_version": provider + "-v1",
            "volume_unit": source["volume_unit"], "library_version": library_version,
            "calendar_id": "szse-month-calendar", "calendar_source_url": CALENDAR_URL,
            "calendar_source_id": "szse_calendar",
            "requested_start": start, "requested_end": end,
            "expected_sessions": [day.isoformat() for day in sessions], "missing_sessions": [],
            "coverage_verified": True, "coverage_unverified": False,
            "row_count": len(bars), "first_date": bars[0].day.isoformat(), "last_date": bars[-1].day.isoformat(),
            "downloaded_at": datetime.now(timezone.utc).isoformat(), "published_at": None,
            "warnings": ["published_at missing; not a verified point-in-time history"],
            "publication_eligible": source.get("public_release_allowed") is True,
            "sha256": hashlib.sha256((staging / "bars.csv").read_bytes()).hexdigest(),
            "calendar_sha256": hashlib.sha256((staging / "calendar.csv").read_bytes()).hexdigest(),
        }
        metadata["dataset_id"] = dataset_identity(metadata)
        (staging / "manifest.json").write_bytes(_json_bytes(metadata))
        findings = scan_privacy(staging)
        if findings:
            raise ValueError("snapshot privacy check failed: " + "; ".join(findings))
        if output.exists():
            raise FileExistsError("snapshot already exists")
        os.rename(staging, output)
    return output
