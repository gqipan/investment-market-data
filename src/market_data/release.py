"""Deterministic candidate packaging and fail-closed release verification."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
import re
import shutil
from pathlib import Path
import tarfile
import tempfile
from typing import Any

from .quality import read_bars, read_calendar, validate_coverage
from .privacy import scan_privacy
from .provenance import check_code_commit
from .catalog import validate_catalog


SCHEMA_VERSION = "1.2"
ADAPTER_VERSION = "fixture-v1"


def _json_bytes(data: Any, *, indent: int | None = 2) -> bytes:
    return (
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            allow_nan=False,
            separators=(",", ":") if indent is None else None,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_identity(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "dataset_id"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_catalog(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / "catalog" / name).read_text(encoding="utf-8"))


def _write_bars(path: Path, bars) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
        for bar in bars:
            writer.writerow(
                [
                    bar.day.isoformat(),
                    repr(bar.open),
                    repr(bar.high),
                    repr(bar.low),
                    repr(bar.close),
                    repr(bar.volume),
                ]
            )


def _write_calendar(path: Path, sessions) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Date", "IsOpen"])
        for day in sessions:
            writer.writerow([day.isoformat(), "true"])


def _tarinfo(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    return info


def _write_reproducible_tar(source: Path, destination: Path, prefix: str) -> None:
    entries = sorted(path for path in source.rglob("*") if path.is_file())
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in entries:
                    relative = path.relative_to(source).as_posix()
                    data = path.read_bytes()
                    info = _tarinfo(f"{prefix}/{relative}")
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))


def _validate_source_for_build(source: dict[str, Any]) -> None:
    if source.get("status") == "restricted":
        raise ValueError("source is restricted and cannot enter the pipeline")


def _private_capability(source: dict[str, Any], name: str) -> bool:
    private_name = f"private_{name}"
    if private_name in source:
        return source.get(private_name) is True
    return source.get(name) is True


def build_candidate(
    *,
    root: Path,
    dataset_name: str,
    as_of: str,
    revision: int,
    output_dir: Path,
    generated_at: str | None = None,
    code_commit: str | None = None,
    snapshot_dir: Path | None = None,
) -> Path:
    if revision < 1:
        raise ValueError("revision must be at least 1")
    instruments = _load_catalog(root, "instruments.json")
    sources = _load_catalog(root, "sources.json")
    datasets = _load_catalog(root, "datasets.json")
    if dataset_name not in datasets:
        raise KeyError(f"unknown dataset: {dataset_name}")
    dataset = datasets[dataset_name]
    instrument = instruments[dataset["instrument"]]
    snapshot = None
    source_id = dataset["source"]
    if snapshot_dir is not None:
        snapshot = _read_snapshot(snapshot_dir, dataset_name, instrument)
        source_id = snapshot["source"]
    source = sources[source_id]
    calendar_source_id = snapshot["calendar_source_id"] if snapshot else dataset.get("calendar_source", source_id)
    calendar_source = sources[calendar_source_id]
    _validate_source_for_build(source)
    _validate_source_for_build(calendar_source)
    if snapshot is None and (not dataset["bars_file"] or not dataset["calendar_file"]):
        raise ValueError("dataset does not have an implemented source adapter")

    bars = read_bars(snapshot_dir / "bars.csv" if snapshot else root / dataset["bars_file"])
    sessions = read_calendar(snapshot_dir / "calendar.csv" if snapshot else root / dataset["calendar_file"])
    validate_coverage(bars, sessions)
    if bars[-1].day.isoformat() != as_of:
        raise ValueError(f"as-of {as_of} does not match last bar {bars[-1].day}")

    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    code_commit = code_commit or os.environ.get("GITHUB_SHA") or "local-uncommitted"
    source_slug = "fixture" if source_id == "synthetic_fixture" else source_id
    release_id = f"data-{as_of}-{source_slug}-r{revision}"
    release_dir = output_dir / release_id
    if release_dir.exists():
        raise FileExistsError(f"release candidate already exists: {release_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{release_id}-", dir=output_dir) as temp:
        staging = Path(temp)
        payload = staging / dataset_name
        payload.mkdir()
        bars_path = payload / "bars.csv"
        calendar_path = payload / "calendar.csv"
        _write_bars(bars_path, bars)
        _write_calendar(calendar_path, sessions)
        manifest: dict[str, Any] = {
            "data_class": dataset["data_class"],
            "synthetic": dataset["data_class"] == "synthetic_fixture",
            "dataset_key": dataset["dataset_key"],
            "symbol": instrument["symbol"],
            "market": instrument["market"],
            "currency": instrument["currency"],
            "source": source_id,
            "source_url": source["usage_url"],
            "series_type": instrument["series_type"],
            "frequency": instrument["frequency"],
            "timezone": instrument["timezone"],
            "adapter_version": ADAPTER_VERSION,
            "volume_unit": dataset["volume_unit"],
            "adjust": instrument["adjust"],
            "published_at": None,
            "calendar_id": "explicit-fixture-calendar",
            "calendar_source_id": calendar_source_id,
            "expected_sessions": [day.isoformat() for day in sessions],
            "missing_sessions": [],
            "coverage_verified": True,
            "coverage_unverified": False,
            "first_date": bars[0].day.isoformat(),
            "last_date": bars[-1].day.isoformat(),
            "row_count": len(bars),
            "warnings": [
                "synthetic fixture only",
                "published_at missing; not a verified point-in-time history",
            ],
            "downloaded_at": generated_at,
            "sha256": _sha256(bars_path),
        }
        if snapshot:
            manifest = dict(snapshot)
            manifest.pop("dataset_id", None)
            manifest["sha256"] = _sha256(bars_path)
            manifest["calendar_sha256"] = _sha256(calendar_path)
        manifest["dataset_id"] = dataset_identity(manifest)
        (payload / "manifest.json").write_bytes(_json_bytes(manifest))
        (payload / "README.txt").write_text(
            "Synthetic pipeline fixture. Not real CSI 300 market data.\n" if not snapshot
            else "Local research price snapshot. Data permissions are separate from code licensing.\n",
            encoding="utf-8",
        )

        asset_name = f"{instrument['symbol']}-daily-price-index.tar.gz"
        asset_path = staging / asset_name
        _write_reproducible_tar(payload, asset_path, dataset_name)
        shutil.rmtree(payload)
        asset_hash = _sha256(asset_path)
        quality = {
            "status": "pass",
            "checks": {
                "bar_count": len(bars),
                "calendar_count": len(sessions),
                "duplicate_dates": False,
                "ohlc_valid": True,
                "coverage_complete": True,
            },
            "warnings": manifest["warnings"],
        }
        release_manifest = {
            "schema_version": SCHEMA_VERSION,
            "release_id": release_id,
            "code_commit": code_commit,
            "generated_at": generated_at,
            "data_class": dataset["data_class"],
            "calendar_source_id": calendar_source_id,
            "private_release_eligible": all(
                s.get("private_release_allowed") is True
                and _private_capability(s, "cloud_collection_allowed")
                and _private_capability(s, "cloud_storage_allowed")
                for s in (source, calendar_source)
            ),
            "publication_eligible": all(s.get("public_release_allowed") is True for s in (source, calendar_source)),
            "source": {
                "id": source_id,
                "status": source["status"],
                "private_release_allowed": bool(source.get("private_release_allowed")),
                "redistribution_allowed": bool(source.get("redistribution_allowed")),
                "cloud_storage_allowed": bool(source.get("cloud_storage_allowed")),
                "usage_url": source["usage_url"],
            },
            "datasets": [
                {
                    "catalog_dataset": dataset_name,
                    "symbol": instrument["symbol"],
                    "dataset_key": snapshot["dataset_key"] if snapshot else dataset["dataset_key"],
                    "dataset_id": manifest["dataset_id"],
                    "asset": asset_name,
                    "size": asset_path.stat().st_size,
                    "sha256": asset_hash,
                    "first_date": manifest["first_date"],
                    "last_date": manifest["last_date"],
                    "row_count": manifest["row_count"],
                }
            ],
        }
        (staging / "release-manifest.json").write_bytes(_json_bytes(release_manifest))
        (staging / "quality-report.json").write_bytes(_json_bytes(quality))

        checksum_targets = [
            asset_name,
            "release-manifest.json",
            "quality-report.json",
        ]
        checksum_text = "".join(
            f"{_sha256(staging / name)}  {name}\n" for name in checksum_targets
        )
        (staging / "SHA256SUMS").write_text(checksum_text, encoding="utf-8")
        os.replace(staging, release_dir)
    return release_dir


def _read_snapshot(directory: Path, dataset_name: str, instrument: dict) -> dict:
    required = {"bars.csv", "calendar.csv", "manifest.json"}
    if {path.name for path in directory.iterdir()} != required:
        raise ValueError("snapshot must contain exactly bars, calendar and manifest")
    if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
        raise ValueError("snapshot files must be regular files")
    findings = scan_privacy(directory)
    if findings:
        raise ValueError("snapshot privacy check failed: " + "; ".join(findings))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != dataset_identity(manifest):
        raise ValueError("snapshot identity mismatch")
    if manifest.get("dataset_name") != dataset_name or manifest.get("symbol") != instrument["symbol"]:
        raise ValueError("snapshot instrument or dataset mismatch")
    for key in ("market", "currency", "series_type", "timezone", "frequency", "adjust"):
        if manifest.get(key) != instrument[key]:
            raise ValueError(f"snapshot instrument metadata mismatch: {key}")
    source_id = manifest.get("source")
    if (source_id not in instrument.get("provider_symbols", {})
            or manifest.get("source_symbol") != instrument["provider_symbols"][source_id]
            or manifest.get("data_class") != "normalized_price" or manifest.get("synthetic") is not False):
        raise ValueError("snapshot provider or data class mismatch")
    if manifest.get("calendar_source_id") != "szse_calendar":
        raise ValueError("snapshot calendar source is not registered")
    if manifest.get("sha256") != _sha256(directory / "bars.csv"):
        raise ValueError("snapshot bars hash mismatch")
    if manifest.get("calendar_sha256") != _sha256(directory / "calendar.csv"):
        raise ValueError("snapshot calendar hash mismatch")
    bars = read_bars(directory / "bars.csv")
    sessions = read_calendar(directory / "calendar.csv")
    validate_coverage(bars, sessions)
    _check_coverage_metadata(manifest, bars, sessions)
    return manifest


def _check_coverage_metadata(manifest, bars, sessions) -> None:
    actual = {"row_count": len(bars), "first_date": bars[0].day.isoformat(),
              "last_date": bars[-1].day.isoformat()}
    if any(manifest.get(key) != value for key, value in actual.items()):
        raise ValueError("coverage metadata mismatch")
    if (manifest.get("expected_sessions") != [day.isoformat() for day in sessions]
            or manifest.get("missing_sessions") != []
            or manifest.get("coverage_verified") is not True
            or manifest.get("coverage_unverified") is not False):
        raise ValueError("calendar metadata mismatch")


def _parse_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parts = line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError(f"{path}:{line_no}: invalid checksum line")
        digest, name = parts
        if Path(name).name != name or name in result:
            raise ValueError(f"{path}:{line_no}: unsafe or duplicate filename")
        result[name] = digest
    if not result:
        raise ValueError(f"{path}: empty checksum file")
    return result


def _safe_extract(archive_path: Path, target: Path) -> None:
    if archive_path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("compressed bundle exceeds 50 MiB")
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = []
        for member in archive:
            members.append(member)
            if len(members) > 100:
                raise ValueError("bundle exceeds 100 members")
        if not members:
            raise ValueError("bundle is empty")
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported archive member: {member.name}")
            if member.size > 50 * 1024 * 1024:
                raise ValueError(f"archive member exceeds 50 MiB: {member.name}")
        if len({Path(member.name) for member in members}) != len(members):
            raise ValueError("duplicate archive member")
        if sum(member.size for member in members) > 100 * 1024 * 1024:
            raise ValueError("expanded bundle exceeds 100 MiB")
        for member in members:
            destination = target / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read archive member: {member.name}")
            with source, destination.open("wb") as handle:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    handle.write(chunk)
            destination.chmod(0o644)


def verify_release(release_dir: Path) -> dict[str, Any]:
    if release_dir.is_symlink() or any(path.is_symlink() for path in release_dir.iterdir()):
        raise ValueError("release symlink is not allowed")
    checksums = _parse_checksums(release_dir / "SHA256SUMS")
    for name, expected in checksums.items():
        path = release_dir / name
        if not path.is_file():
            raise ValueError(f"missing release asset: {name}")
        if _sha256(path) != expected:
            raise ValueError(f"checksum mismatch: {name}")
    release_manifest = json.loads(
        (release_dir / "release-manifest.json").read_text(encoding="utf-8")
    )
    if release_manifest.get("release_id") != release_dir.name:
        raise ValueError("release id does not match directory")
    if release_manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported candidate schema; rebuild the candidate")
    datasets = release_manifest.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 1:
        raise ValueError("first release must contain exactly one dataset")
    dataset = datasets[0]
    asset_name = dataset.get("asset")
    if not isinstance(asset_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz", asset_name):
        raise ValueError("unsafe release asset path")
    required = {"release-manifest.json", "quality-report.json", asset_name}
    if set(checksums) != required:
        raise ValueError("release checksum file must cover exactly the release files")
    if {path.name for path in release_dir.iterdir()} != required | {"SHA256SUMS"}:
        raise ValueError("unexpected release file")
    if any(path.is_symlink() or not path.is_file() for path in release_dir.iterdir()):
        raise ValueError("release files must be regular files")
    for name in ("release-manifest.json", "quality-report.json"):
        _assert_private_text_free(release_dir / name)
    asset_path = release_dir / dataset["asset"]
    if asset_path.stat().st_size != dataset["size"]:
        raise ValueError("release asset size mismatch")
    if _sha256(asset_path) != dataset["sha256"]:
        raise ValueError("release asset hash mismatch")

    with tempfile.TemporaryDirectory(prefix="verify-release-") as temp:
        root = Path(temp)
        _safe_extract(asset_path, root)
        manifests = list(root.rglob("manifest.json"))
        bars_paths = list(root.rglob("bars.csv"))
        calendars = list(root.rglob("calendar.csv"))
        if len(manifests) != 1 or len(bars_paths) != 1 or len(calendars) != 1:
            raise ValueError("bundle must contain one manifest, bars file, and calendar")
        payload = manifests[0].parent
        if payload.relative_to(root).parts != (dataset.get("catalog_dataset"),):
            raise ValueError("bundle directory does not match catalog dataset")
        expected_files = {payload / name for name in ("manifest.json", "bars.csv", "calendar.csv", "README.txt")}
        if {path for path in root.rglob("*") if path.is_file()} != expected_files:
            raise ValueError("unexpected bundle file")
        findings = scan_privacy(root, _archive_depth=1)
        if findings:
            raise ValueError("bundle privacy check failed: " + "; ".join(findings))
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        if manifest.get("dataset_id") != dataset["dataset_id"]:
            raise ValueError("bundle dataset_id mismatch")
        if manifest.get("sha256") != _sha256(bars_paths[0]):
            raise ValueError("bundle bars hash mismatch")
        if manifest.get("dataset_id") != dataset_identity(manifest):
            raise ValueError("bundle manifest identity mismatch")
        if (manifest.get("symbol") != dataset.get("symbol")
                or manifest.get("dataset_key") != dataset.get("dataset_key")
                or manifest.get("data_class") != release_manifest.get("data_class")):
            raise ValueError("bundle dataset metadata mismatch")
        bars = read_bars(bars_paths[0])
        sessions = read_calendar(calendars[0])
        validate_coverage(bars, sessions)
        actual = {"row_count": len(bars), "first_date": bars[0].day.isoformat(),
                  "last_date": bars[-1].day.isoformat()}
        for key, value in actual.items():
            if manifest.get(key) != value or dataset.get(key) != value:
                raise ValueError(f"bundle coverage metadata mismatch: {key}")
        _check_coverage_metadata(manifest, bars, sessions)
        if manifest.get("calendar_sha256") and manifest["calendar_sha256"] != _sha256(calendars[0]):
            raise ValueError("bundle calendar hash mismatch")
        if manifest.get("source") != release_manifest.get("source", {}).get("id"):
            raise ValueError("bundle source metadata mismatch")
        if manifest.get("calendar_source_id") != release_manifest.get("calendar_source_id"):
            raise ValueError("bundle calendar source metadata mismatch")
        quality = json.loads((release_dir / "quality-report.json").read_text(encoding="utf-8"))
        if (quality.get("status") != "pass"
                or quality.get("checks", {}).get("bar_count") != len(bars)
                or quality.get("checks", {}).get("calendar_count") != len(sessions)):
            raise ValueError("quality report metadata mismatch")
    return release_manifest


def _assert_private_text_free(path: Path) -> None:
    # Scan only the text being distributed here; archives are scanned after safe extraction.
    with tempfile.TemporaryDirectory(prefix="privacy-text-") as temp:
        target = Path(temp) / path.name
        target.write_bytes(path.read_bytes())
        findings = scan_privacy(Path(temp))
    if findings:
        raise ValueError("release privacy check failed: " + "; ".join(findings))


def assert_release_allowed(root: Path, release_dir: Path, scope: str) -> None:
    release_manifest = verify_release(release_dir)
    if (release_manifest.get("data_class") == "synthetic_fixture"
            or release_manifest.get("source", {}).get("id") == "synthetic_fixture"):
        raise ValueError("release is blocked: synthetic data cannot be published")
    if release_manifest.get("code_commit") in (None, "", "local-uncommitted"):
        raise ValueError("release is blocked: code commit is not verified")
    validate_catalog(root)
    datasets = _load_catalog(root, "datasets.json")
    item = release_manifest["datasets"][0]
    dataset = datasets.get(item.get("catalog_dataset"))
    if not dataset or dataset["data_class"] != release_manifest["data_class"]:
        raise ValueError("release is blocked: catalog dataset does not match candidate")
    instruments = _load_catalog(root, "instruments.json")
    instrument = instruments[dataset["instrument"]]
    actual_source = release_manifest["source"]["id"]
    if actual_source == dataset["source"]:
        expected_key = dataset["dataset_key"]
    elif actual_source in instrument.get("provider_symbols", {}):
        expected_key = f"{instrument['canonical_id']}.{actual_source}"
    else:
        raise ValueError("release is blocked: provider is not registered for catalog instrument")
    if item.get("symbol") != instrument["symbol"] or item.get("dataset_key") != expected_key:
        raise ValueError("release is blocked: catalog instrument or key does not match candidate")
    sources = _load_catalog(root, "sources.json")
    source_id = release_manifest["source"]["id"]
    source = sources[source_id]
    calendar_source = sources.get(release_manifest.get("calendar_source_id"))
    if not calendar_source:
        raise ValueError("release is blocked: calendar source is not registered")
    findings = scan_privacy(root)
    if findings:
        raise ValueError("repository privacy check failed: " + "; ".join(findings))
    if scope == "private":
        if not release_manifest.get("private_release_eligible"):
            raise ValueError("release is blocked: private release is not approved")
        if source.get("status") not in {"approved_private", "approved_public"}:
            raise ValueError(
                f"release is blocked: source status is {source.get('status')}"
            )
        if source.get("private_release_allowed") is not True:
            raise ValueError("release is blocked: private release is not approved")
        if not _private_capability(source, "cloud_collection_allowed"):
            raise ValueError("release is blocked: cloud collection is not approved")
        if not _private_capability(source, "cloud_storage_allowed"):
            raise ValueError("release is blocked: private cloud storage is not approved")
        _assert_calendar_allowed(calendar_source, scope)
        check_code_commit(root, release_manifest["code_commit"])
        return
    if scope == "public":
        if not release_manifest.get("publication_eligible"):
            raise ValueError("release is blocked: public release is not approved")
        if source.get("status") != "approved_public":
            raise ValueError(
                f"release is blocked: source status is {source.get('status')}"
            )
        if source.get("public_release_allowed") is not True:
            raise ValueError("release is blocked: public release is not approved")
        if source.get("cloud_collection_allowed") is not True:
            raise ValueError("release is blocked: cloud collection is not approved")
        if source.get("redistribution_allowed") is not True:
            raise ValueError("release is blocked: redistribution is not approved")
        if source.get("cloud_storage_allowed") is not True:
            raise ValueError("release is blocked: cloud storage is not approved")
        _assert_calendar_allowed(calendar_source, scope)
        check_code_commit(root, release_manifest["code_commit"])
        return
    raise ValueError(f"unknown release scope: {scope}")


def _assert_calendar_allowed(source: dict, scope: str) -> None:
    statuses = {"approved_private", "approved_public"} if scope == "private" else {"approved_public"}
    keys = (f"{scope}_release_allowed",)
    if scope == "public":
        keys += ("cloud_collection_allowed", "cloud_storage_allowed", "redistribution_allowed")
    if source.get("status") not in statuses or any(source.get(key) is not True for key in keys):
        raise ValueError(f"release is blocked: calendar source is not approved for {scope} release")
    if scope == "private" and not (
        _private_capability(source, "cloud_collection_allowed")
        and _private_capability(source, "cloud_storage_allowed")
    ):
        raise ValueError(f"release is blocked: calendar source is not approved for {scope} release")


def assert_publication_allowed(root: Path, release_dir: Path) -> None:
    """Backward-compatible helper for the public release gate."""
    assert_release_allowed(root, release_dir, "public")
