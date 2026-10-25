"""Build immutable cumulative release trees from a baseline and daily deltas."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

from .privacy import scan_privacy
from .quality import read_bars, read_calendar, validate_coverage
from .release import _json_bytes, dataset_identity
from .valuation import verify_valuation


MANIFEST_NAME = "cumulative-manifest.json"
IGNORED_NAMES = {"request-cache"}


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
