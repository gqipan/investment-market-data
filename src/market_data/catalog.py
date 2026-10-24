"""Structural validation for instruments, sources, datasets, and watchlists."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MARKET_DEFAULTS = {
    "CN": ("CNY", "Asia/Shanghai"),
    "HK": ("HKD", "Asia/Hong_Kong"),
    "US": ("USD", "America/New_York"),
}
SOURCE_STATUSES = {
    "approved_for_tests",
    "approved_private",
    "approved_public",
    "pending_authorization",
    "restricted",
}
PERMISSION_KEYS = (
    "private_release_allowed",
    "public_release_allowed",
    "cloud_collection_allowed",
    "cloud_storage_allowed",
    "redistribution_allowed",
    "private_cloud_collection_allowed",
    "private_cloud_storage_allowed",
)
VALUATION_FIELDS = {"Date", "Metric", "Value", "Unit", "Method"}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_object(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except FileNotFoundError as exc:
        raise ValueError(f"missing catalog file: {relative}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {relative}: {exc}") from exc
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{relative} must contain a non-empty JSON object")
    return value


def _relative_data_file(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe catalog data path: {value}")
    path = root / relative
    if not path.is_file():
        raise ValueError(f"catalog data file does not exist: {value}")
    return path


def validate_catalog(root: Path) -> dict[str, int]:
    instruments = _load_object(root, "catalog/instruments.json")
    sources = _load_object(root, "catalog/sources.json")
    datasets = _load_object(root, "catalog/datasets.json")
    watchlist = _load_object(root, "catalog/watchlist.json")

    canonical_ids: set[str] = set()
    symbols: set[str] = set()
    for instrument_id, instrument in instruments.items():
        if not isinstance(instrument, dict):
            raise ValueError(f"instrument {instrument_id} must be an object")
        required = (
            "canonical_id",
            "display_name",
            "symbol",
            "market",
            "currency",
            "asset_type",
            "series_type",
            "timezone",
            "frequency",
            "adjust",
        )
        missing = [field for field in required if not instrument.get(field)]
        if missing:
            raise ValueError(
                f"instrument {instrument_id} is missing fields: {', '.join(missing)}"
            )
        canonical_id = instrument["canonical_id"]
        symbol = instrument["symbol"]
        if canonical_id in canonical_ids:
            raise ValueError(f"duplicate canonical_id: {canonical_id}")
        if symbol in symbols:
            raise ValueError(f"duplicate instrument symbol: {symbol}")
        canonical_ids.add(canonical_id)
        symbols.add(symbol)

        market = instrument["market"]
        if market not in MARKET_DEFAULTS:
            raise ValueError(f"instrument {instrument_id} has unknown market: {market}")
        expected_currency, expected_timezone = MARKET_DEFAULTS[market]
        if instrument["currency"] != expected_currency:
            raise ValueError(
                f"instrument {instrument_id} currency must be {expected_currency}"
            )
        if instrument["timezone"] != expected_timezone:
            raise ValueError(
                f"instrument {instrument_id} timezone must be {expected_timezone}"
            )
        if instrument["frequency"] != "1d" or instrument["adjust"] != "raw":
            raise ValueError(
                f"instrument {instrument_id} must be daily raw data in this schema"
            )

    for source_id, source in sources.items():
        if not isinstance(source, dict):
            raise ValueError(f"source {source_id} must be an object")
        status = source.get("status")
        if status not in SOURCE_STATUSES:
            raise ValueError(f"source {source_id} has unknown status: {status}")
        for key in PERMISSION_KEYS:
            if key in source and not isinstance(source.get(key), bool):
                raise ValueError(f"source {source_id}.{key} must be boolean")
        usage_url = source.get("usage_url")
        if usage_url is not None and not isinstance(usage_url, str):
            raise ValueError(f"source {source_id}.usage_url must be a string or null")
        if status in {"approved_private", "approved_public"} and not usage_url:
            raise ValueError(
                f"approved source {source_id} must record a source usage URL"
            )

    dataset_keys: set[str] = set()
    for dataset_id, dataset in datasets.items():
        if not isinstance(dataset, dict):
            raise ValueError(f"dataset {dataset_id} must be an object")
        required = ("instrument", "source", "dataset_key", "data_class", "label")
        missing = [field for field in required if not dataset.get(field)]
        if missing:
            raise ValueError(
                f"dataset {dataset_id} is missing fields: {', '.join(missing)}"
            )
        if dataset["instrument"] not in instruments:
            raise ValueError(
                f"dataset {dataset_id} references unknown instrument: "
                f"{dataset['instrument']}"
            )
        if dataset["source"] not in sources:
            raise ValueError(
                f"dataset {dataset_id} references unknown source: {dataset['source']}"
            )
        dataset_key = dataset["dataset_key"]
        if dataset_key in dataset_keys:
            raise ValueError(f"duplicate dataset_key: {dataset_key}")
        dataset_keys.add(dataset_key)

        bars_file = dataset.get("bars_file")
        calendar_file = dataset.get("calendar_file")
        if dataset["data_class"] == "valuation_observation":
            fields = dataset.get("fields")
            if not isinstance(fields, list) or not fields:
                raise ValueError(
                    f"valuation dataset {dataset_id} must declare non-empty fields"
                )
            if set(fields) != VALUATION_FIELDS:
                raise ValueError(
                    f"valuation dataset {dataset_id} must use the normalized observation fields"
                )
        if bars_file is not None:
            _relative_data_file(root, bars_file)
        if calendar_file is not None:
            _relative_data_file(root, calendar_file)

        source = sources[dataset["source"]]
        if source["status"] in {"approved_private", "approved_public"}:
            if not dataset.get("adapter") and (not bars_file or not calendar_file):
                raise ValueError(
                    f"approved dataset {dataset_id} has no enabled data adapter"
                )

    tracked_symbols = watchlist.get("symbols")
    if not isinstance(tracked_symbols, list) or not tracked_symbols:
        raise ValueError("catalog/watchlist.json symbols must be a non-empty list")
    if len(tracked_symbols) != len(set(tracked_symbols)):
        raise ValueError("catalog/watchlist.json contains duplicate symbols")
    required_data_classes = watchlist.get("required_data_classes")
    if (
        not isinstance(required_data_classes, list)
        or not required_data_classes
        or not all(isinstance(item, str) and item for item in required_data_classes)
    ):
        raise ValueError(
            "catalog/watchlist.json required_data_classes must be a non-empty string list"
        )

    instruments_by_symbol = {
        instrument["symbol"]: instrument_id
        for instrument_id, instrument in instruments.items()
    }
    datasets_by_instrument: dict[str, list[dict[str, Any]]] = {}
    for dataset in datasets.values():
        datasets_by_instrument.setdefault(dataset["instrument"], []).append(dataset)

    for symbol in tracked_symbols:
        instrument_id = instruments_by_symbol.get(symbol)
        if instrument_id is None:
            raise ValueError(f"watchlist symbol {symbol} has no instrument")
        available_classes = {
            dataset["data_class"]
            for dataset in datasets_by_instrument.get(instrument_id, [])
        }
        for data_class in required_data_classes:
            if data_class not in available_classes:
                raise ValueError(
                    f"watchlisted instrument {instrument_id} has no {data_class} dataset"
                )

    return {
        "instruments": len(instruments),
        "sources": len(sources),
        "datasets": len(datasets),
        "tracked": len(tracked_symbols),
    }


def build_catalog_report(root: Path) -> dict[str, Any]:
    counts = validate_catalog(root)
    instruments = _load_object(root, "catalog/instruments.json")
    sources = _load_object(root, "catalog/sources.json")
    datasets = _load_object(root, "catalog/datasets.json")
    watchlist = _load_object(root, "catalog/watchlist.json")

    by_symbol = {
        instrument["symbol"]: (instrument_id, instrument)
        for instrument_id, instrument in instruments.items()
    }
    by_instrument_class: dict[tuple[str, str], list[str]] = {}
    for dataset_id, dataset in datasets.items():
        key = (dataset["instrument"], dataset["data_class"])
        by_instrument_class.setdefault(key, []).append(dataset_id)

    tracked: list[dict[str, Any]] = []
    for symbol in watchlist["symbols"]:
        instrument_id, instrument = by_symbol[symbol]
        entry: dict[str, Any] = {
            "symbol": symbol,
            "display_name": instrument["display_name"],
            "instrument": instrument_id,
            "canonical_id": instrument["canonical_id"],
        }
        for data_class in watchlist["required_data_classes"]:
            dataset_ids = by_instrument_class.get((instrument_id, data_class), [])
            if len(dataset_ids) != 1:
                raise ValueError(
                    f"{instrument_id} must have exactly one {data_class} dataset"
                )
            dataset = datasets[dataset_ids[0]]
            source = sources[dataset["source"]]
            entry[data_class] = {
                "dataset_id": dataset_ids[0],
                "dataset_key": dataset["dataset_key"],
                "source_id": dataset["source"],
                "source_status": source["status"],
                "adapter_configured": bool(
                    dataset.get("adapter") or (dataset.get("bars_file") and dataset.get("calendar_file"))
                )
                if data_class == "normalized_price"
                else False,
            }
        tracked.append(entry)

    return {
        "schema_version": "1.0",
        "watchlist": watchlist["name"],
        "market_data_included": False,
        "source_authorization_complete": False,
        "counts": counts,
        "tracked_indices": tracked,
    }
