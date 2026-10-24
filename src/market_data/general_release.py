"""Pinned releases for normalized close histories and valuation observations."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from .catalog import validate_catalog
from .history import verify_close_history
from .privacy import scan_privacy
from .provenance import check_code_commit
from .release import (
    _assert_private_text_free,
    _json_bytes,
    _parse_checksums,
    _safe_extract,
    _sha256,
    _write_reproducible_tar,
    dataset_identity,
)
from .valuation import verify_valuation


SCHEMA_VERSION = "1.3"
DATA_FILES = {
    "normalized_close": ("close.csv", "raw-bars.json", "manifest.json"),
    "valuation_observation": ("observations.csv", "raw.json", "manifest.json"),
}
RELEASE_ID_RE = re.compile(
    r"data-[0-9]{4}-[0-9]{2}-[0-9]{2}-[A-Za-z0-9_-]+-r[1-9][0-9]*"
)


def _load(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / "catalog" / name).read_text(encoding="utf-8"))


def _snapshot_metadata(snapshot: Path, data_class: str) -> tuple[dict[str, Any], list[str]]:
    files = DATA_FILES.get(data_class)
    if files is None:
        raise ValueError("unsupported snapshot data class")
    snapshot = snapshot.resolve()
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("snapshot must be a regular directory")
    if {path.name for path in snapshot.iterdir()} != set(files):
        raise ValueError("snapshot must contain exactly its schema files")
    if any(path.is_symlink() or not path.is_file() for path in snapshot.iterdir()):
        raise ValueError("snapshot files must be regular files")
    findings = scan_privacy(snapshot)
    if findings:
        raise ValueError("snapshot privacy check failed: " + "; ".join(findings))
    meta = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if meta.get("data_class") != data_class or meta.get("synthetic") is not False:
        raise ValueError("snapshot class or synthetic flag is incompatible")
    if meta.get("dataset_id") != dataset_identity(meta):
        raise ValueError("snapshot dataset identity mismatch")
    if data_class == "normalized_close":
        verify_close_history(snapshot)
    else:
        verify_valuation(snapshot)
    return meta, list(files)


def _source_eligibility(source: dict[str, Any], *, public: bool) -> bool:
    key = "public_release_allowed" if public else "private_release_allowed"
    common = (
        source.get(key),
        source.get("cloud_collection_allowed"),
        source.get("cloud_storage_allowed"),
    )
    if public:
        common += (source.get("redistribution_allowed"),)
    return all(value is True for value in common)


def _snapshot_span(
    snapshot: Path, meta: dict[str, Any], data_class: str
) -> tuple[str, str, int]:
    if data_class == "normalized_close":
        return meta["first_date"], meta["last_date"], meta["row_count"]
    with (snapshot / "observations.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    days = [row["Date"] for row in rows]
    return min(days), max(days), len(rows)


def build_snapshot_candidate(
    *,
    root: Path,
    dataset_name: str,
    snapshot_dir: Path,
    as_of: str,
    revision: int,
    output_dir: Path,
    generated_at: str | None = None,
    code_commit: str | None = None,
) -> Path:
    """Build one deterministic release for a verified close or valuation snapshot."""
    if revision < 1:
        raise ValueError("revision must be at least 1")
    root = root.resolve()
    validate_catalog(root)
    instruments = _load(root, "instruments.json")
    sources = _load(root, "sources.json")
    datasets = _load(root, "datasets.json")
    dataset = datasets.get(dataset_name)
    if dataset is None:
        raise KeyError(f"unknown dataset: {dataset_name}")
    instrument = instruments[dataset["instrument"]]
    snapshot = snapshot_dir.resolve()
    preview = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    data_class = preview.get("data_class")
    catalog_class = (
        "normalized_price"
        if data_class == "normalized_close"
        else "valuation_observation"
    )
    if data_class not in DATA_FILES or dataset["data_class"] != catalog_class:
        raise ValueError("catalog dataset does not support the snapshot data class")
    meta, files = _snapshot_metadata(snapshot, data_class)
    first_date, last_date, row_count = _snapshot_span(snapshot, meta, data_class)
    if meta.get("symbol") != instrument["symbol"] or last_date != as_of:
        raise ValueError("snapshot instrument or as-of does not match catalog dataset")
    source_id = meta.get("source")
    if source_id not in sources:
        raise ValueError("snapshot source is not registered")
    if (
        source_id != dataset["source"]
        and source_id not in instrument.get("provider_symbols", {})
    ):
        raise ValueError("snapshot provider is not registered for the instrument")
    source = sources[source_id]
    calendar_source_id = (
        "szse_calendar" if data_class == "normalized_close" else None
    )
    calendar_source = sources.get(calendar_source_id) if calendar_source_id else None
    if calendar_source_id and calendar_source is None:
        raise ValueError("calendar source is not registered")

    generated_at = generated_at or meta.get("downloaded_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("generated_at is required")
    code_commit = code_commit or os.environ.get("GITHUB_SHA") or "local-uncommitted"
    release_id = f"data-{as_of}-{source_id}-r{revision}"
    release_dir = output_dir / release_id
    if release_dir.exists():
        raise FileExistsError(f"release candidate already exists: {release_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{release_id}-", dir=output_dir) as temp:
        staging = Path(temp)
        payload = staging / dataset_name
        payload.mkdir()
        for name in files:
            shutil.copyfile(snapshot / name, payload / name)
        (payload / "README.txt").write_text(
            "Pinned market history snapshot. Source permissions remain separate from code licensing.\n",
            encoding="utf-8",
        )

        asset_name = f"{instrument['symbol']}-{data_class}.tar.gz"
        asset_path = staging / asset_name
        _write_reproducible_tar(payload, asset_path, dataset_name)
        shutil.rmtree(payload)
        source_public = _source_eligibility(source, public=True)
        source_private = _source_eligibility(source, public=False)
        if calendar_source:
            source_public = source_public and _source_eligibility(calendar_source, public=True)
            source_private = source_private and _source_eligibility(calendar_source, public=False)
        release_manifest = {
            "schema_version": SCHEMA_VERSION,
            "release_id": release_id,
            "code_commit": code_commit,
            "generated_at": generated_at,
            "data_class": data_class,
            "source": {
                "id": source_id,
                "status": source["status"],
                "private_release_allowed": bool(source.get("private_release_allowed")),
                "public_release_allowed": bool(source.get("public_release_allowed")),
                "redistribution_allowed": bool(source.get("redistribution_allowed")),
                "cloud_storage_allowed": bool(source.get("cloud_storage_allowed")),
                "usage_url": source.get("usage_url"),
            },
            "calendar_source_id": calendar_source_id,
            "private_release_eligible": source_private,
            "publication_eligible": source_public,
            "datasets": [
                {
                    "catalog_dataset": dataset_name,
                    "symbol": instrument["symbol"],
                    "dataset_key": dataset["dataset_key"],
                    "dataset_id": meta["dataset_id"],
                    "asset": asset_name,
                    "size": asset_path.stat().st_size,
                    "sha256": _sha256(asset_path),
                    "first_date": first_date,
                    "last_date": last_date,
                    "row_count": row_count,
                }
            ],
        }
        quality_checks: dict[str, Any] = {
            "dataset_verified": True,
            "dataset_id": meta["dataset_id"],
        }
        if data_class == "normalized_close":
            quality_checks.update(
                row_count=row_count,
                coverage_verified=meta["coverage_verified"],
                missing_sessions=meta["missing_sessions"],
                ohlcv_validation_passed=False,
            )
        else:
            metrics_summary = {
                metric: {
                    key: details.get(key)
                    for key in (
                        "row_count",
                        "first_date",
                        "last_date",
                        "frequency",
                        "unit",
                        "method",
                        "requested_sessions_covered",
                    )
                }
                for metric, details in meta["metrics"].items()
            }
            for metric, details in metrics_summary.items():
                missing = meta["metrics"][metric].get("missing_sessions")
                unexpected = meta["metrics"][metric].get("unexpected_dates")
                details["missing_session_count"] = len(missing) if missing is not None else None
                details["unexpected_date_count"] = len(unexpected) if unexpected is not None else None
            quality_checks.update(
                observation_count=row_count,
                metrics=metrics_summary,
            )
        quality = {
            "status": "pass",
            "data_class": data_class,
            "checks": quality_checks,
            "warnings": meta.get("warnings", [meta.get("warning")]),
        }
        (staging / "release-manifest.json").write_bytes(_json_bytes(release_manifest))
        (staging / "quality-report.json").write_bytes(_json_bytes(quality))
        checksum_names = [asset_name, "release-manifest.json", "quality-report.json"]
        (staging / "SHA256SUMS").write_text(
            "".join(
                f"{_sha256(staging / name)}  {name}\n"
                for name in checksum_names
            ),
            encoding="utf-8",
        )
        os.replace(staging, release_dir)
    verify_snapshot_release(release_dir)
    return release_dir


def verify_snapshot_release(release_dir: Path) -> dict[str, Any]:
    """Verify a self-contained close or valuation release without repo policy state."""
    release_dir = release_dir.resolve()
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise ValueError("release must be a regular directory")
    if any(path.is_symlink() or not path.is_file() for path in release_dir.iterdir()):
        raise ValueError("release files must be regular files")
    checksums = _parse_checksums(release_dir / "SHA256SUMS")
    for name, expected in checksums.items():
        path = release_dir / name
        if not path.is_file() or _sha256(path) != expected:
            raise ValueError(f"release checksum mismatch: {name}")
    raw = (release_dir / "release-manifest.json").read_bytes()
    header = json.loads(raw)
    if (
        header.get("schema_version") != SCHEMA_VERSION
        or header.get("release_id") != release_dir.name
        or RELEASE_ID_RE.fullmatch(release_dir.name) is None
    ):
        raise ValueError("unsupported or mismatched general release identity")
    datasets = header.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 1:
        raise ValueError("general release must contain exactly one dataset")
    item = datasets[0]
    if not isinstance(item, dict):
        raise ValueError("invalid general release dataset")
    asset_name = item.get("asset")
    if (
        not isinstance(asset_name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz", asset_name) is None
    ):
        raise ValueError("unsafe general release asset")
    required = {"release-manifest.json", "quality-report.json", asset_name}
    if set(checksums) != required or set(p.name for p in release_dir.iterdir()) != required | {"SHA256SUMS"}:
        raise ValueError("unexpected general release files")
    for name in ("release-manifest.json", "quality-report.json"):
        _assert_private_text_free(release_dir / name)
    asset_path = release_dir / asset_name
    if asset_path.stat().st_size != item.get("size") or _sha256(asset_path) != item.get("sha256"):
        raise ValueError("general release asset size or hash mismatch")
    data_class = header.get("data_class")
    expected_names = DATA_FILES.get(data_class)
    if expected_names is None:
        raise ValueError("unsupported general release data class")

    with tempfile.TemporaryDirectory(prefix="verify-general-release-") as temp:
        root = Path(temp)
        _safe_extract(asset_path, root)
        manifests = list(root.rglob("manifest.json"))
        if len(manifests) != 1:
            raise ValueError("bundle must contain exactly one snapshot manifest")
        payload = manifests[0].parent
        if payload.relative_to(root).parts != (item.get("catalog_dataset"),):
            raise ValueError("bundle directory does not match catalog dataset")
        expected_files = {payload / name for name in (*expected_names, "README.txt")}
        if {path for path in root.rglob("*") if path.is_file()} != expected_files:
            raise ValueError("unexpected general release bundle file")
        findings = scan_privacy(root, _archive_depth=1)
        if findings:
            raise ValueError("general release privacy check failed: " + "; ".join(findings))
        meta = json.loads((payload / "manifest.json").read_text(encoding="utf-8"))
        if (
            meta.get("dataset_id") != item.get("dataset_id")
            or meta.get("dataset_id") != dataset_identity(meta)
            or meta.get("data_class") != data_class
            or meta.get("source") != header.get("source", {}).get("id")
        ):
            raise ValueError("bundle dataset identity or source mismatch")
        if data_class == "normalized_close":
            verify_close_history(payload)
        else:
            verify_valuation(payload)
        first_date, last_date, row_count = _snapshot_span(payload, meta, data_class)
        for key in ("first_date", "last_date", "row_count"):
            expected = {
                "first_date": first_date,
                "last_date": last_date,
                "row_count": row_count,
            }[key]
            if item.get(key) != expected:
                raise ValueError(f"bundle coverage metadata mismatch: {key}")
        quality = json.loads((release_dir / "quality-report.json").read_text(encoding="utf-8"))
        if (
            quality.get("status") != "pass"
            or quality.get("data_class") != data_class
            or quality.get("checks", {}).get("dataset_id") != meta.get("dataset_id")
        ):
            raise ValueError("general release quality report mismatch")
    return header


def stage_snapshot_release(
    *,
    release_dir: Path,
    expected_manifest_sha256: str,
    expected_dataset_id: str,
    expected_data_class: str,
    cache_dir: Path,
    allow_synthetic: bool = False,
) -> Path:
    """Install one verified snapshot release by its manifest hash; do not change pointers."""
    for value in (expected_manifest_sha256, expected_dataset_id):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("manifest and dataset pins must be SHA-256")
    if expected_data_class not in DATA_FILES:
        raise ValueError("unsupported expected data class")
    release_dir = release_dir.resolve()
    manifest_path = release_dir / "release-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("release manifest is missing or unsafe")
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError("release manifest hash does not match the caller pin")
    header = json.loads(raw)
    if header.get("data_class") != expected_data_class:
        raise ValueError("release data class does not match the caller pin")
    if header.get("data_class") == "synthetic_fixture":
        if not allow_synthetic:
            raise ValueError("synthetic data requires explicit smoke-test mode")
    elif header.get("publication_eligible") is not True:
        raise ValueError("release is not approved for public consumption")
    verified = verify_snapshot_release(release_dir)
    if verified.get("datasets", [{}])[0].get("dataset_id") != expected_dataset_id:
        raise ValueError("release dataset does not match the caller pin")
    cache_dir = cache_dir.resolve()
    target = cache_dir / expected_manifest_sha256
    if target.exists():
        _verify_staged_snapshot(target, expected_dataset_id, expected_data_class)
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-general-", dir=cache_dir) as temp:
        staging = Path(temp) / "snapshot"
        source = Path(temp) / "unpacked"
        item = verified["datasets"][0]
        _safe_extract(release_dir / item["asset"], source)
        payload = source / item["catalog_dataset"]
        staging.mkdir()
        for name in DATA_FILES[expected_data_class]:
            shutil.copyfile(payload / name, staging / name)
        _verify_staged_snapshot(staging, expected_dataset_id, expected_data_class)
        try:
            os.rename(staging, target)
        except OSError:
            if not target.exists():
                raise
            _verify_staged_snapshot(target, expected_dataset_id, expected_data_class)
    return target


def _verify_staged_snapshot(
    directory: Path, dataset_id: str, data_class: str
) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("cached snapshot must be a regular directory")
    expected = set(DATA_FILES[data_class])
    if {path.name for path in directory.iterdir()} != expected:
        raise ValueError("cached snapshot has unexpected files")
    meta, _ = _snapshot_metadata(directory, data_class)
    if meta.get("dataset_id") != dataset_id:
        raise ValueError("cached snapshot dataset identity mismatch")


def assert_snapshot_release_allowed(root: Path, release_dir: Path, scope: str) -> None:
    """Apply current source and provenance policy to a close/valuation release."""
    root = root.resolve()
    header = verify_snapshot_release(release_dir)
    if header.get("source", {}).get("id") == "synthetic_fixture":
        raise ValueError("release is blocked: synthetic data cannot be published")
    if header.get("code_commit") in (None, "", "local-uncommitted"):
        raise ValueError("release is blocked: code commit is not verified")
    validate_catalog(root)
    instruments = _load(root, "instruments.json")
    datasets = _load(root, "datasets.json")
    sources = _load(root, "sources.json")
    item = header["datasets"][0]
    dataset = datasets.get(item.get("catalog_dataset"))
    if dataset is None:
        raise ValueError("release is blocked: catalog dataset is missing")
    instrument = instruments[dataset["instrument"]]
    if item.get("symbol") != instrument["symbol"]:
        raise ValueError("release is blocked: instrument does not match release")
    source_id = header["source"]["id"]
    if source_id != dataset["source"] and source_id not in instrument.get("provider_symbols", {}):
        raise ValueError("release is blocked: provider is not registered")
    source = sources.get(source_id)
    if source is None:
        raise ValueError("release is blocked: source is not registered")
    calendar_source = sources.get(header.get("calendar_source_id")) if header.get("calendar_source_id") else None
    if header.get("calendar_source_id") and calendar_source is None:
        raise ValueError("release is blocked: calendar source is not registered")
    findings = scan_privacy(root)
    if findings:
        raise ValueError("repository privacy check failed: " + "; ".join(findings))
    eligible = header.get(f"{scope}_release_eligible" if scope == "private" else "publication_eligible")
    if eligible is not True:
        raise ValueError(f"release is blocked: {scope} release is not approved")
    statuses = {"approved_private", "approved_public"} if scope == "private" else {"approved_public"}
    if source.get("status") not in statuses:
        raise ValueError(f"release is blocked: source status is {source.get('status')}")
    permission_key = "private_release_allowed" if scope == "private" else "public_release_allowed"
    required = (permission_key, "cloud_collection_allowed", "cloud_storage_allowed")
    if scope == "public":
        required += ("redistribution_allowed",)
    if any(source.get(key) is not True for key in required):
        raise ValueError(f"release is blocked: source is not approved for {scope} release")
    if calendar_source:
        if calendar_source.get("status") not in statuses or any(
            calendar_source.get(key) is not True for key in required
        ):
            raise ValueError(f"release is blocked: calendar source is not approved for {scope} release")
    check_code_commit(root, header["code_commit"])
