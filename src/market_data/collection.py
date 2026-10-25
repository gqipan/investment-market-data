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
SINA_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
CSINDEX_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
CALENDAR_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
PROVIDERS = {"tencent_raw", "sina_raw", "akshare_eastmoney", "csindex_official"}


def request_json(url: str, params: dict):
    if "csindex.com.cn" in url:
        referer = "https://www.csindex.com.cn/"
    elif "szse.cn" in url:
        referer = "https://www.szse.cn/"
    elif "finance.sina.com.cn" in url:
        referer = "https://finance.sina.com.cn/"
    else:
        referer = "https://gu.qq.com/"
    request = Request(url + "?" + urlencode(params), headers={
        "User-Agent": "Mozilla/5.0", "Referer": referer,
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


def fetch_calendar(
    start: date,
    end: date,
    *,
    request=request_json,
    allow_empty: bool = False,
) -> list[date]:
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
    if not sessions and not allow_empty:
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


def _sina_bars(symbol: str, start: date, end: date, request) -> list[Bar]:
    """Read Sina's raw index day series and retain only the requested range.

    The endpoint accepts a trailing row count, not date bounds. Requesting its
    documented 2,000-row maximum preserves enough history for the 850-session
    research window without inventing rows before the endpoint's retained
    prefix. Requested-range coverage is validated independently below.
    """
    payload = request(
        SINA_URL,
        {"symbol": symbol, "scale": 240, "ma": "no", "datalen": 2000},
    )
    if not isinstance(payload, list) or not payload:
        raise ValueError("Sina raw index response is missing")
    bars = []
    for row in payload:
        if not isinstance(row, dict) or not {"day", "open", "high", "low", "close", "volume"} <= set(row):
            raise ValueError("Sina raw index fields are incomplete")
        day = date.fromisoformat(str(row["day"])[:10])
        if day < start or day > end:
            continue
        values = [float(row[key]) for key in ("open", "high", "low", "close", "volume")]
        bars.append(Bar(day, *values))
    if not bars:
        raise ValueError("Sina raw index series has no rows in the requested range")
    return bars


def _csindex_bars(
    symbol: str, start: date, end: date, sessions: list[date], request
) -> tuple[list[Bar], list[date]]:
    """Read official CSI OHLCV and ignore only explicit no-trade close snapshots."""
    payload = request(
        CSINDEX_URL,
        {
            "indexCode": symbol,
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
        },
    )
    if not isinstance(payload, dict) or str(payload.get("code")) != "200":
        raise ValueError("CSI official raw index response failed")
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("CSI official raw index series is missing")
    expected = set(sessions)
    bars: list[Bar] = []
    ignored_closed: list[date] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("CSI official raw index row is invalid")
        try:
            day = date(
                int(str(row["tradeDate"])[:4]),
                int(str(row["tradeDate"])[4:6]),
                int(str(row["tradeDate"])[6:8]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("CSI official raw index date is invalid") from exc
        if not start <= day <= end:
            continue
        if day not in expected:
            unchanged_close = (
                row.get("open") is None
                and row.get("high") is None
                and row.get("low") is None
                and float(row.get("tradingVol") or 0) == 0
                and float(row.get("close") or 0) > 0
            )
            if not unchanged_close:
                raise ValueError("CSI official returned an unexpected trading-session date")
            ignored_closed.append(day)
            continue
        try:
            values = [
                float(row[key])
                for key in ("open", "high", "low", "close", "tradingVol")
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("CSI official raw index fields are incomplete") from exc
        bars.append(Bar(day, *values))
    if not bars:
        raise ValueError("CSI official raw index series has no trading-session rows")
    return bars, ignored_closed


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
    cloud_allowed = source.get("cloud_collection_allowed") is True or (
        os.environ.get("MARKET_DATA_PRIVATE_CLOUD") == "true"
        and source.get("private_cloud_collection_allowed") is True
    )
    if os.environ.get("GITHUB_ACTIONS") == "true" and not cloud_allowed:
        raise ValueError("cloud collection is not approved")
    if calendar_source.get("local_collection_allowed") is not True:
        raise ValueError("local calendar collection is not enabled")
    calendar_cloud_allowed = calendar_source.get("cloud_collection_allowed") is True or (
        os.environ.get("MARKET_DATA_PRIVATE_CLOUD") == "true"
        and calendar_source.get("private_cloud_collection_allowed") is True
    )
    if os.environ.get("GITHUB_ACTIONS") == "true" and not calendar_cloud_allowed:
        raise ValueError("cloud calendar collection is not approved")
    if instrument.get("asset_type") != "index" or instrument.get("series_type") not in {
        "price_index", "total_return_index",
    }:
        raise ValueError("collector accepts registered price or total-return indices only")
    symbol = instrument.get("provider_symbols", {}).get(provider)
    if not symbol:
        raise ValueError("index provider symbol is not explicitly registered")
    sessions = fetch_calendar(first, last, request=request)
    library_version = None
    ignored_closed_observations: list[date] = []
    if provider == "tencent_raw":
        bars = _tencent_bars(symbol, first, last, request)
        source_url = TENCENT_URL
    elif provider == "sina_raw":
        bars = _sina_bars(symbol, first, last, request)
        source_url = SINA_URL
    elif provider == "csindex_official":
        bars, ignored_closed_observations = _csindex_bars(
            symbol, first, last, sessions, request
        )
        source_url = CSINDEX_URL
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
            "history_start": instrument.get("history_start"),
            "history_start_basis": instrument.get("history_start_basis"),
            "published_from": instrument.get("published_from"),
            "ignored_closed_observations": [
                day.isoformat() for day in ignored_closed_observations
            ],
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
