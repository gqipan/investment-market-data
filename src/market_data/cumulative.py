"""Build immutable cumulative release trees from a baseline and daily deltas."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import csv
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from typing import Any

from .privacy import scan_privacy
from .quality import Bar, read_bars, read_calendar, validate_coverage
from .release import _json_bytes, dataset_identity
from .valuation import verify_valuation


MANIFEST_NAME = "cumulative-manifest.json"
IGNORED_NAMES = {"request-cache"}
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024


def _archive_member_name(member: tarfile.TarInfo) -> str | None:
    """Return a normalized safe path, or None for macOS metadata sidecars."""
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    path = PurePosixPath(name)
    if path.name.startswith("._") or "__MACOSX" in path.parts:
        return None
    if name in {"", "."}:
        if member.isdir():
            return None
        raise ValueError(f"unsafe cumulative archive path: {member.name}")
    if (
        path.is_absolute()
        or ".." in path.parts
        or name.startswith("/")
    ):
        raise ValueError(f"unsafe cumulative archive path: {member.name}")
    return path.as_posix()


def extract_cumulative_archive(archive_path: Path, output_dir: Path) -> None:
    """Safely extract a cumulative archive, ignoring inert macOS metadata files."""
    archive_path = Path(archive_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise FileExistsError("cumulative extraction output must be absent or empty")
    with tarfile.open(archive_path, mode="r:gz") as archive:
        safe_members: list[tuple[tarfile.TarInfo, str]] = []
        seen: set[str] = set()
        extracted_bytes = 0
        for count, member in enumerate(archive, start=1):
            if count > MAX_ARCHIVE_MEMBERS:
                raise ValueError("cumulative archive contains too many members")
            name = _archive_member_name(member)
            if name is None:
                continue
            if (
                member.issym()
                or member.islnk()
                or member.ischr()
                or member.isblk()
                or member.isfifo()
                or member.isdev()
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"cumulative archive contains a link or special file: {name}")
            if name in seen:
                raise ValueError(f"cumulative archive contains a duplicate path: {name}")
            seen.add(name)
            if member.isfile():
                extracted_bytes += member.size
                if extracted_bytes > MAX_EXTRACTED_BYTES:
                    raise ValueError("extracted cumulative archive exceeds the size limit")
            safe_members.append((member, name))
        if not safe_members:
            raise ValueError("cumulative archive is empty")

        output_dir.mkdir(parents=True, exist_ok=True)
        for member, name in safe_members:
            destination = output_dir / name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read cumulative archive member: {name}")
            with source, destination.open("wb") as handle:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    handle.write(chunk)
            destination.chmod(0o644)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"cumulative symlink is forbidden: {path.relative_to(root)}")
        if path.is_file() and path.name != MANIFEST_NAME:
            relative = path.relative_to(root).as_posix()
            entries.append({
                "path": relative,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            })
    return entries


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ValueError("cumulative input must be a real directory")
    target.mkdir(parents=True)
    for path in sorted(source.iterdir()):
        if path.name in IGNORED_NAMES:
            continue
        if path.is_symlink():
            raise ValueError(f"cumulative symlink is forbidden: {path.name}")
        destination = target / path.name
        if path.is_dir():
            shutil.copytree(path, destination)
        elif path.is_file():
            shutil.copy2(path, destination)
        else:
            raise ValueError(f"unsupported cumulative input: {path.name}")


def verify_cumulative(root: Path) -> dict[str, Any]:
    """Verify every file covered by a cumulative manifest."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("cumulative root must be a real directory")
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("cumulative manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "cumulative-market-data-1":
        raise ValueError("unknown cumulative manifest schema")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("cumulative manifest has no file inventory")
    expected = {entry.get("path") for entry in entries}
    if None in expected or len(expected) != len(entries):
        raise ValueError("cumulative manifest has duplicate or invalid paths")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual != expected:
        raise ValueError("cumulative file inventory mismatch")
    for entry in entries:
        path = root / entry["path"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("cumulative inventory contains a missing or linked file")
        if path.stat().st_size != entry.get("size") or _sha256(path) != entry.get("sha256"):
            raise ValueError(f"cumulative file hash mismatch: {entry['path']}")
    dates = [entry.get("as_of") for entry in manifest.get("batches", [])]
    if dates != sorted(set(dates)):
        raise ValueError("cumulative batch dates must be unique and sorted")
    return manifest


def verify_registry_cache(root: Path, registry_path: Path) -> dict[str, int]:
    """Verify every content-addressed dataset referenced by a private registry."""
    root = root.resolve()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = registry.get("datasets")
    if not isinstance(entries, list) or not entries:
        raise ValueError("registry has no datasets")
    counts = {"normalized_price": 0, "normalized_close": 0, "valuation_observation": 0}
    seen: set[str] = set()
    for entry in entries:
        cache_name = entry.get("cache_name")
        dataset_id = entry.get("dataset_id")
        data_class = entry.get("data_class")
        if data_class not in counts:
            raise ValueError(f"unknown registry data class: {data_class}")
        if not isinstance(cache_name, str) or len(cache_name) != 64 or cache_name in seen:
            raise ValueError("registry contains an invalid or duplicate cache name")
        seen.add(cache_name)
        store = root / cache_name
        manifest_path = store / "manifest.json"
        if store.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError(f"registry cache is missing: {cache_name}")
        if _sha256(manifest_path) != cache_name:
            raise ValueError(f"registry manifest hash mismatch: {cache_name}")
        if entry.get("manifest_sha256") != cache_name:
            raise ValueError(f"registry pin mismatch: {cache_name}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("dataset_id") != dataset_id or dataset_identity(manifest) != dataset_id:
            raise ValueError(f"dataset identity mismatch: {cache_name}")
        if (
            manifest.get("data_class") != data_class
            or manifest.get("source") != entry.get("source")
            or manifest.get("symbol") != entry.get("symbol")
        ):
            raise ValueError(f"registry metadata mismatch: {cache_name}")
        if data_class == "normalized_price":
            if {path.name for path in store.iterdir()} != {"bars.csv", "calendar.csv", "manifest.json"}:
                raise ValueError(f"unexpected normalized price files: {cache_name}")
            if _sha256(store / "bars.csv") != manifest.get("sha256"):
                raise ValueError(f"bars hash mismatch: {cache_name}")
            if _sha256(store / "calendar.csv") != manifest.get("calendar_sha256"):
                raise ValueError(f"calendar hash mismatch: {cache_name}")
            validate_coverage(read_bars(store / "bars.csv"), read_calendar(store / "calendar.csv"))
        elif data_class == "normalized_close":
            from .history import verify_close_history
            verify_close_history(store)
        else:
            verify_valuation(store)
        findings = scan_privacy(store)
        if findings:
            raise ValueError(f"registry cache privacy failure: {cache_name}")
        counts[data_class] += 1
    return {"datasets": len(entries), **counts}


def _batch_snapshots(root: Path, batches: list[dict[str, Any]]) -> dict[tuple, list[tuple[str, Path, dict]]]:
    """Return verified snapshots grouped by logical route in batch order."""
    grouped: dict[tuple[str, str, str], list[tuple[str, Path, dict]]] = {}
    root = root.resolve()
    for batch in batches:
        batch_path = (root / batch["path"]).resolve()
        if not batch_path.is_relative_to(root) or not batch_path.is_dir():
            raise ValueError("cumulative batch path is invalid")
        verify_delta_tree(batch_path)
        snapshots = sorted(
            path
            for path in batch_path.rglob("manifest.json")
            if "request-cache" not in path.relative_to(batch_path).parts
        )
        seen: set[tuple[str, str, str]] = set()
        for manifest_path in snapshots:
            snapshot = manifest_path.parent
            meta = json.loads(manifest_path.read_text(encoding="utf-8"))
            route = (meta["symbol"], meta["source"], meta["data_class"])
            if route in seen:
                raise ValueError(
                    f"cumulative batch contains duplicate route: {route[0]}/{route[1]}/{route[2]}"
                )
            seen.add(route)
            grouped.setdefault(route, []).append((batch["as_of"], snapshot, meta))
    return grouped


def _merge_calendar_contexts(snapshots: list[tuple[str, Path, dict]]) -> dict | None:
    """Never turn disjoint calendar evidence into a claimed continuous range."""
    contexts = [item[2].get('calendar_context') for item in snapshots]
    if not all(contexts):
        return None
    contexts.sort(key=lambda context: context['requested_start'])
    first = contexts[0]['requested_start']
    covered_end = date.fromisoformat(contexts[0]['requested_end'])
    sessions = set(contexts[0]['expected_sessions'])
    for context in contexts[1:]:
        start = date.fromisoformat(context['requested_start'])
        end = date.fromisoformat(context['requested_end'])
        if start > covered_end + timedelta(days=1):
            raise ValueError('calendar context gap between verified intervals')
        overlap_end = min(covered_end, end).isoformat()
        old_overlap = {day for day in sessions if start.isoformat() <= day <= overlap_end}
        new_overlap = {day for day in context['expected_sessions'] if start.isoformat() <= day <= overlap_end}
        if old_overlap != new_overlap:
            raise ValueError('calendar context conflict in overlapping interval')
        sessions.update(context['expected_sessions'])
        covered_end = max(covered_end, end)
    return {'source_id': 'szse_calendar', 'requested_start': first,
            'requested_end': covered_end.isoformat(), 'expected_sessions': sorted(sessions)}


def _merge_price_snapshots(
    snapshots: list[tuple[str, Path, dict]],
    target: Path,
) -> dict[str, Any]:
    """Merge adjacent same-source batches into one verified price snapshot."""
    calendar_context = _merge_calendar_contexts(snapshots)
    bars_by_day: dict[date, Bar] = {}
    sessions_by_day: dict[date, bool] = {}
    for _, snapshot, meta in snapshots:
        for bar in read_bars(snapshot / "bars.csv"):
            current = bars_by_day.get(bar.day)
            if current is not None and current != bar:
                raise ValueError(f"overlapping price rows differ on {bar.day.isoformat()}")
            bars_by_day[bar.day] = bar
        for day in read_calendar(snapshot / "calendar.csv"):
            sessions_by_day[day] = True
    bars = [bars_by_day[day] for day in sorted(bars_by_day)]
    sessions = sorted(sessions_by_day)
    validate_coverage(bars, sessions)
    target.mkdir()
    from .release import _write_bars, _write_calendar

    _write_bars(target / "bars.csv", bars)
    _write_calendar(target / "calendar.csv", sessions)
    meta = dict(snapshots[-1][2])
    meta.pop("dataset_id", None)
    meta.update(
        requested_start=min(item[2]["requested_start"] for item in snapshots),
        requested_end=max(item[2]["requested_end"] for item in snapshots),
        expected_sessions=[day.isoformat() for day in sessions],
        missing_sessions=[],
        coverage_verified=True,
        coverage_unverified=False,
        row_count=len(bars),
        first_date=bars[0].day.isoformat(),
        last_date=bars[-1].day.isoformat(),
        sha256=_sha256(target / "bars.csv"),
        calendar_sha256=_sha256(target / "calendar.csv"),
    )
    if calendar_context is not None:
        meta['calendar_context'] = calendar_context
    else:
        meta.pop('calendar_context', None)
    meta["dataset_id"] = dataset_identity(meta)
    (target / "manifest.json").write_bytes(_json_bytes(meta))
    _verify_price_snapshot(target)
    return meta


def _merge_close_snapshots(
    snapshots: list[tuple[str, Path, dict]],
    target: Path,
) -> dict[str, Any]:
    """Merge close-only batches without inventing OHLC fields."""
    closes: dict[str, float] = {}
    raw_rows: dict[str, list] = {}
    expected: set[str] = set()
    anomalies: dict[str, dict] = {}
    errata: set[str] = set()
    for _, snapshot, meta in snapshots:
        with (snapshot / "close.csv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                day = date.fromisoformat(row["Date"]).isoformat()
                value = float(row["Close"])
                current = closes.get(day)
                if current is not None and current != value:
                    raise ValueError(f"overlapping close rows differ on {day}")
                closes[day] = value
        raw = json.loads((snapshot / "raw-bars.json").read_text(encoding="utf-8"))
        for row in raw.get("rows", []):
            if not isinstance(row, list) or not row:
                raise ValueError("invalid raw close-history row")
            day = date.fromisoformat(str(row[0])[:10]).isoformat()
            current = raw_rows.get(day)
            if current is not None and current != row:
                raise ValueError(f"overlapping raw close-history rows differ on {day}")
            raw_rows[day] = row
        expected.update(meta["expected_sessions"])
        for item in meta.get("ohlc_anomalies", []):
            anomalies[item["date"]] = item
        errata.update(
            (meta.get("calendar_errata") or {}).get("closed_dates", [])
        )
    if not raw_rows:
        raise ValueError("normalized close history has no raw rows")
    target.mkdir()
    with (target / "close.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Date", "Close"])
        writer.writerows(
            (day, format(closes[day], ".15g")) for day in sorted(closes)
        )
    (target / "raw-bars.json").write_bytes(
        _json_bytes({"rows": [raw_rows[day] for day in sorted(raw_rows)]})
    )
    days = sorted(closes)
    missing = sorted(expected - set(days))
    meta = dict(snapshots[-1][2])
    meta.pop("dataset_id", None)
    latest_errata = dict(meta.get("calendar_errata") or {})
    latest_errata["closed_dates"] = sorted(errata)
    meta.update(
        requested_start=min(item[2]["requested_start"] for item in snapshots),
        requested_end=max(item[2]["requested_end"] for item in snapshots),
        first_date=days[0],
        last_date=days[-1],
        row_count=len(days),
        expected_sessions=sorted(expected),
        missing_sessions=missing,
        coverage_verified=not missing,
        ohlc_anomalies=[anomalies[day] for day in sorted(anomalies)],
        calendar_errata=latest_errata,
        files={
            name: _sha256(target / name)
            for name in ("close.csv", "raw-bars.json")
        },
    )
    meta["dataset_id"] = dataset_identity(meta)
    (target / "manifest.json").write_bytes(_json_bytes(meta))
    from .history import verify_close_history

    verify_close_history(target)
    return meta


def _extend_close_from_prices(target: Path, prices: list[tuple[str, Path, dict]]) -> None:
    """Append genuine same-route Close observations, retaining close-only identity.

    A request interval must bridge any working days after the prior request.
    Holidays must therefore be included in a verified requested range; no
    weekday is silently inferred closed. Weekends need no fabricated sessions.
    """
    from .history import verify_close_history

    meta = verify_close_history(target)
    if not meta['coverage_verified']:
        raise ValueError('close projection cannot hide an existing coverage gap')
    with (target / 'close.csv').open(encoding='utf-8', newline='') as handle:
        closes = {row['Date']: float(row['Close']) for row in csv.DictReader(handle)}
    raw = json.loads((target / 'raw-bars.json').read_text(encoding='utf-8'))
    projections = list(meta.get('close_projections', []))
    raw_projections = list(raw.get('close_projections', []))
    expected = set(meta['expected_sessions'])
    covered_end = date.fromisoformat(meta['requested_end'])
    for _, snapshot, price in sorted(prices, key=lambda item: item[2]['requested_start']):
        if (price['symbol'], price['source']) != (meta['symbol'], meta['source']):
            raise ValueError('close projection route mismatch')
        _verify_price_snapshot(snapshot)
        first = date.fromisoformat(price['requested_start'])
        last = date.fromisoformat(price['requested_end'])
        day = covered_end + timedelta(days=1)
        context = price.get('calendar_context')
        if context and date.fromisoformat(context['requested_start']) <= day:
            bridge = {session for session in context['expected_sessions']
                      if covered_end.isoformat() < session < first.isoformat()}
            if bridge - set(closes):
                raise ValueError('close projection calendar gap: ' + ','.join(sorted(bridge - set(closes))))
        else:
            # Compatibility for old verified manifests without calendar context:
            # only a pure weekend bridge is known independently to be closed.
            while day < first:
                if day.weekday() < 5:
                    raise ValueError(f'close projection calendar gap on {day.isoformat()}')
                day += timedelta(days=1)
        rows = []
        for bar in read_bars(snapshot / 'bars.csv'):
            day_text = bar.day.isoformat()
            if day_text in closes and closes[day_text] != bar.close:
                raise ValueError(f'overlapping close rows differ on {day_text}')
            # A daily tail must not claim unvalidated new historical prefix.
            if day_text < meta['first_date']:
                continue
            closes[day_text] = bar.close
            rows.append([day_text, bar.close])
        expected.update(day for day in price['expected_sessions'] if day >= meta['first_date'])
        covered_end = max(covered_end, last)
        evidence = {'dataset_id': price['dataset_id'], 'source': price['source'],
                    'symbol': price['symbol'], 'sha256': price['sha256'],
                    'calendar_sha256': price['calendar_sha256'],
                    'requested_start': price['requested_start'], 'requested_end': price['requested_end']}
        projections.append(evidence)
        raw_projections.append({**evidence, 'rows': rows, 'fields': ['Date', 'Close']})
    if expected != set(closes):
        raise ValueError('close projection coverage gap')
    with (target / 'close.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(['Date', 'Close'])
        writer.writerows((day, format(closes[day], '.15g')) for day in sorted(closes))
    raw['close_projections'] = raw_projections
    (target / 'raw-bars.json').write_bytes(_json_bytes(raw))
    meta.update(last_date=max(closes), row_count=len(closes), requested_end=covered_end.isoformat(),
                expected_sessions=sorted(expected), close_projections=projections,
                files={name: _sha256(target / name) for name in ('close.csv', 'raw-bars.json')})
    meta['dataset_id'] = dataset_identity(meta)
    (target / 'manifest.json').write_bytes(_json_bytes(meta))
    verify_close_history(target)


def _merge_valuation_snapshots(
    snapshots: list[tuple[str, Path, dict]],
    target: Path,
) -> dict[str, Any]:
    """Merge same-source observations and reject conflicting overlapping values."""
    observations: dict[tuple[str, str], dict[str, str]] = {}
    raw_batches = []
    sessions: set[str] = set()
    versions = []
    for batch_as_of, snapshot, meta in snapshots:
        with (snapshot / "observations.csv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = (row["Date"], row["Metric"])
                current = observations.get(key)
                if current is not None and current != row:
                    raise ValueError(
                        f"overlapping valuation rows differ on {row['Date']}/{row['Metric']}"
                    )
                observations[key] = row
        raw_batches.append({
            "as_of": batch_as_of,
            "dataset_id": meta["dataset_id"],
            "raw": json.loads((snapshot / "raw.json").read_text(encoding="utf-8")),
        })
        sessions.update(meta.get("calendar_sessions") or [])
        if meta.get("library_version"):
            versions.append(str(meta["library_version"]))
    rows = [observations[key] for key in sorted(observations)]
    from .valuation import write_valuation

    write_valuation(
        target,
        rows,
        symbol=snapshots[-1][2]["symbol"],
        source=snapshots[-1][2]["source"],
        raw={"batches": raw_batches},
        requested_start=min(item[2]["requested_start"] for item in snapshots),
        requested_end=max(item[2]["requested_end"] for item in snapshots),
        library_version=versions[-1] if versions else None,
        sessions=sorted(sessions) or None,
    )
    return json.loads((target / "manifest.json").read_text(encoding="utf-8"))


def materialize_cumulative(root: Path, output_dir: Path) -> dict[str, Any]:
    """Combine ordered batches into current snapshots and a private research registry."""
    root = root.resolve()
    output_dir = output_dir.resolve()
    source_manifest = verify_cumulative(root)
    if output_dir.exists():
        raise FileExistsError("materialized cumulative output already exists")
    grouped = _batch_snapshots(root, source_manifest["batches"])
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".materialized-cumulative-", dir=output_dir.parent) as temp:
        staging = Path(temp) / "output"
        staging.mkdir()
        datasets = staging
        entries = []
        for route in sorted(grouped):
            snapshots = grouped[route]
            data_class = route[2]
            working = Path(temp) / "working"
            if len(snapshots) == 1:
                shutil.copytree(snapshots[0][1], working)
            elif data_class == "normalized_price":
                _merge_price_snapshots(snapshots, working)
            elif data_class == "normalized_close":
                _merge_close_snapshots(snapshots, working)
            elif data_class == "valuation_observation":
                _merge_valuation_snapshots(snapshots, working)
            else:
                raise ValueError(f"unknown data class: {data_class}")
            if data_class == "normalized_close":
                prices = grouped.get((route[0], route[1], "normalized_price"), [])
                if prices:
                    _extend_close_from_prices(working, prices)
            raw = (working / "manifest.json").read_bytes()
            cache_name = hashlib.sha256(raw).hexdigest()
            meta = json.loads(raw)
            final = datasets / cache_name
            if final.exists():
                raise ValueError(f"materialized dataset hash collision: {cache_name}")
            os.replace(working, final)
            entry = {
                "symbol": meta["symbol"],
                "source": meta["source"],
                "data_class": meta["data_class"],
                "cache_name": cache_name,
                "manifest_sha256": cache_name,
                "dataset_id": meta["dataset_id"],
            }
            if meta.get("history_start") is not None:
                entry["history_start"] = meta["history_start"]
            entries.append(entry)
        registry = {
            "schema_version": "local-research-registry-1",
            "promotion_allowed": False,
            "expected_session": source_manifest["as_of"],
            "datasets": entries,
            "usage": "local_research_only",
        }
        preview = _write_registry_preview(staging, registry)
        verify_registry_cache(datasets, preview)
        preview.unlink()
        _write_json_atomic(staging / "registry.json", registry)
        os.replace(staging, output_dir)
    return {
        "root": output_dir,
        "registry": output_dir / "registry.json",
        "expected_session": source_manifest["as_of"],
        "datasets": len(entries),
    }


def _write_registry_preview(root: Path, registry: dict[str, Any]) -> Path:
    path = root / ".registry-preview.json"
    path.write_bytes(_json_bytes(registry))
    return path


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def _verify_price_snapshot(path: Path) -> dict[str, Any]:
    """Verify one normalized-price snapshot before it enters a cumulative release."""
    if path.is_symlink() or any(
        (path / name).is_symlink()
        for name in ("bars.csv", "calendar.csv", "manifest.json")
    ):
        raise ValueError("price snapshot symlinks are forbidden")
    if {item.name for item in path.iterdir()} != {
        "bars.csv",
        "calendar.csv",
        "manifest.json",
    }:
        raise ValueError("unexpected price snapshot files")
    meta = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if (
        meta.get("data_class") != "normalized_price"
        or meta.get("synthetic") is not False
    ):
        raise ValueError("unknown or synthetic price snapshot")
    if meta.get("dataset_id") != dataset_identity(meta):
        raise ValueError("price snapshot identity mismatch")
    if _sha256(path / "bars.csv") != meta.get("sha256"):
        raise ValueError("price snapshot bars hash mismatch")
    if _sha256(path / "calendar.csv") != meta.get("calendar_sha256"):
        raise ValueError("price snapshot calendar hash mismatch")
    bars = read_bars(path / "bars.csv")
    calendar = read_calendar(path / "calendar.csv")
    validate_coverage(bars, calendar)
    if (
        [day.isoformat() for day in calendar] != meta.get("expected_sessions")
        or len(bars) != meta.get("row_count")
        or bars[0].day.isoformat() != meta.get("first_date")
        or bars[-1].day.isoformat() != meta.get("last_date")
        or meta.get("coverage_verified") is not True
        or meta.get("missing_sessions") != []
    ):
        raise ValueError("price snapshot metadata coverage mismatch")
    requested_start = date.fromisoformat(meta["requested_start"])
    requested_end = date.fromisoformat(meta["requested_end"])
    context = meta.get('calendar_context')
    if context is not None:
        context_start = date.fromisoformat(context['requested_start'])
        context_end = date.fromisoformat(context['requested_end'])
        context_days = [date.fromisoformat(day) for day in context['expected_sessions']]
        if (context.get('source_id') != 'szse_calendar'
                or context_start > requested_start or context_end < requested_end
                or context_days != sorted(set(context_days))
                or any(not context_start <= day <= context_end for day in context_days)
                or [day for day in context_days if requested_start <= day <= requested_end] != calendar):
            raise ValueError('price snapshot calendar context mismatch')
    if any(not requested_start <= day <= requested_end for day in calendar):
        raise ValueError("price snapshot calendar is outside the requested range")
    return meta


def verify_delta_tree(root: Path) -> dict[str, int]:
    """Verify every dataset manifest in a collected daily or baseline batch."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("delta root must be a real directory")
    counts = {
        "normalized_price": 0,
        "normalized_close": 0,
        "valuation_observation": 0,
    }
    manifests = sorted(
        path
        for path in root.rglob("manifest.json")
        if "request-cache" not in path.relative_to(root).parts
    )
    if not manifests:
        raise ValueError("delta batch has no dataset manifests")
    for manifest_path in manifests:
        snapshot = manifest_path.parent
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
        data_class = meta.get("data_class")
        if data_class == "normalized_price":
            _verify_price_snapshot(snapshot)
        elif data_class == "normalized_close":
            from .history import verify_close_history

            verify_close_history(snapshot)
        elif data_class == "valuation_observation":
            verify_valuation(snapshot)
        else:
            raise ValueError(f"unknown dataset class in delta batch: {data_class}")
        counts[data_class] += 1
    return {"datasets": len(manifests), **counts}


def build_cumulative(
    *,
    delta_dir: Path,
    output_dir: Path,
    as_of: str,
    previous_dir: Path | None = None,
    previous_release: str | None = None,
    previous_archive_sha256: str | None = None,
) -> dict[str, Any]:
    """Create the next full cumulative tree, treating each day as an immutable batch."""
    batch_date = date.fromisoformat(as_of).isoformat()
    output_dir = output_dir.resolve()
    delta_dir = delta_dir.resolve()
    if output_dir.exists():
        raise FileExistsError("cumulative output already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    verify_delta_tree(delta_dir)

    if previous_dir is None:
        if previous_release is not None or previous_archive_sha256 is not None:
            raise ValueError("bootstrap cannot specify a previous release")
        batches = [{"as_of": batch_date, "kind": "baseline", "path": "baseline"}]
        copy_plan = [(delta_dir, output_dir / "baseline")]
    else:
        if not previous_release or not isinstance(previous_archive_sha256, str) or len(previous_archive_sha256) != 64:
            raise ValueError("incremental update must pin its previous release and archive hash")
        previous_dir = previous_dir.resolve()
        previous = verify_cumulative(previous_dir)
        if batch_date in {entry["as_of"] for entry in previous["batches"]}:
            raise ValueError(f"cumulative batch already contains {batch_date}")
        if batch_date <= previous["as_of"]:
            raise ValueError("incremental batch must follow the previous release")
        shutil.copytree(previous_dir, output_dir)
        batches = list(previous["batches"]) + [{
            "as_of": batch_date,
            "kind": "delta",
            "path": f"deltas/{batch_date}",
        }]
        copy_plan = [(delta_dir, output_dir / "deltas" / batch_date)]

    staging = output_dir.parent / (output_dir.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    if previous_dir is None:
        staging.mkdir()
        copy_plan = [(delta_dir, staging / "baseline")]
    else:
        os.replace(output_dir, staging)
        copy_plan = [(delta_dir, staging / "deltas" / batch_date)]

    for source, target in copy_plan:
        _copy_tree(source, target)

    existing_manifest = staging / MANIFEST_NAME
    previous_manifest = (
        json.loads(existing_manifest.read_text(encoding="utf-8"))
        if existing_manifest.is_file() else {}
    )
    manifest = {
        "schema_version": "cumulative-market-data-1",
        "as_of": batch_date,
        "baseline_as_of": previous_manifest.get("baseline_as_of", batch_date),
        "previous_release": previous_release,
        "previous_archive_sha256": previous_archive_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batches": batches,
        "files": _files(staging),
    }
    existing_manifest.write_bytes(_json_bytes(manifest))
    findings = scan_privacy(staging)
    if findings:
        raise ValueError("cumulative privacy check failed: " + "; ".join(findings))
    verify_cumulative(staging)
    os.replace(staging, output_dir)
    return manifest


def revise_cumulative(*, previous_dir: Path, repair_dir: Path, output_dir: Path,
                      as_of: str, previous_release: str, previous_archive_sha256: str,
                      root: Path | None = None) -> dict[str, Any]:
    """Append missing same-session routes without replacing any prior dataset.

    This is an explicit immutable release revision, never a new dated batch.
    The caller supplies the checked predecessor archive hash and release tag.
    Materialization must pass before the revised tree becomes visible.
    """
    as_of = date.fromisoformat(as_of).isoformat()
    if (not re.fullmatch(r'data-' + re.escape(as_of) + r'-r[1-9][0-9]*', previous_release)
            or not re.fullmatch(r'[0-9a-f]{64}', previous_archive_sha256)):
        raise ValueError('revision must pin a same-session predecessor and archive hash')
    previous_dir, repair_dir, output_dir = map(lambda path: Path(path).resolve(),
                                              (previous_dir, repair_dir, output_dir))
    previous = verify_cumulative(previous_dir)
    if previous['as_of'] != as_of:
        raise ValueError('revision session must match previous cumulative session')
    if output_dir.exists():
        raise FileExistsError('revision output already exists')
    verified = verify_delta_tree(repair_dir)
    if verified['normalized_close'] or verified['valuation_observation']:
        raise ValueError('source repair accepts only normalized price snapshots')
    catalog_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[2]
    catalog = json.loads((catalog_root / 'catalog/datasets.json').read_text())
    instruments = json.loads((catalog_root / 'catalog/instruments.json').read_text())
    watchlist = json.loads((catalog_root / 'catalog/watchlist.json').read_text())
    allowed_routes = {
        (instruments[catalog[name]['instrument']]['symbol'], catalog[name]['source'], catalog[name]['data_class'])
        for name in watchlist.get('additional_price_datasets', [])
        if instruments[catalog[name]['instrument']]['symbol'] in watchlist['symbols']
    }
    repair_snapshots = sorted(repair_dir.rglob('manifest.json'))
    for path in repair_snapshots:
        meta = json.loads(path.read_text(encoding='utf-8'))
        route = (meta.get('symbol'), meta.get('source'), meta.get('data_class'))
        if (meta.get('source') != 'sina_raw' or meta.get('last_date') != as_of
                or route not in allowed_routes):
            raise ValueError('source repair must contain registered current independent Sina prices')
    # The latest batch must not already own any repaired route. Never overwrite
    # or duplicate a snapshot within that batch, even when the rows agree.
    latest = previous['batches'][-1]
    existing = _batch_snapshots(previous_dir, [latest])
    incoming = _batch_snapshots(repair_dir, [{'as_of': as_of, 'path': '.'}])
    if set(existing) & set(incoming):
        raise ValueError('source repair contains a duplicate route in the current batch')
    repair_pin = hashlib.sha256(_json_bytes(_files(repair_dir))).hexdigest()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.cumulative-revision-', dir=output_dir.parent) as temp:
        staging = Path(temp) / 'tree'
        _copy_tree(previous_dir, staging)
        target = staging / latest['path'] / 'repairs' / repair_pin
        _copy_tree(repair_dir, target)
        manifest = dict(previous)
        manifest.update(previous_release=previous_release, previous_archive_sha256=previous_archive_sha256,
                        generated_at=datetime.now(timezone.utc).isoformat())
        manifest['revisions'] = [*previous.get('revisions', []), {
            'as_of': as_of, 'previous_release': previous_release,
            'previous_archive_sha256': previous_archive_sha256,
            'repair_path': target.relative_to(staging).as_posix(),
            'added_routes': [list(route) for route in sorted(incoming)],
        }]
        manifest['files'] = _files(staging)
        (staging / MANIFEST_NAME).write_bytes(_json_bytes(manifest))
        verify_cumulative(staging)
        if scan_privacy(staging):
            raise ValueError('cumulative revision privacy check failed')
        materialize_cumulative(staging, Path(temp) / 'materialized')
        os.replace(staging, output_dir)
    return manifest
