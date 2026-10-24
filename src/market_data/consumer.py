"""Pin and stage a release for a legacy domestic-index reader; never touch a ledger."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .privacy import scan_privacy
from .quality import read_bars, read_calendar, validate_coverage
from .release import (
    _check_coverage_metadata, _json_bytes, _safe_extract, _sha256,
    dataset_identity, verify_release,
)


ADAPTER = "legacy-cn-reader-v1"


def _check_export(directory: Path, manifest_pin: str, upstream_id: str, expected_export_id: str) -> None:
    if directory.is_symlink() or {path.name for path in directory.iterdir()} != {"bars.csv", "calendar.csv", "manifest.json"}:
        raise ValueError("cached dataset has unexpected files")
    if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
        raise ValueError("cached dataset files must be regular files")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("upstream_release_manifest_sha256") != manifest_pin
            or manifest.get("upstream_dataset_id") != upstream_id
            or manifest.get("consumer_adapter_version") != ADAPTER
            or manifest.get("market") != "A"
            or manifest.get("dataset_id") != expected_export_id
            or manifest.get("dataset_id") != dataset_identity(manifest)):
        raise ValueError("cached dataset identity mismatch")
    if manifest.get("sha256") != _sha256(directory / "bars.csv"):
        raise ValueError("cached dataset bars hash mismatch")
    if manifest.get("calendar_sha256") != _sha256(directory / "calendar.csv"):
        raise ValueError("cached dataset calendar hash mismatch")
    bars, sessions = read_bars(directory / "bars.csv"), read_calendar(directory / "calendar.csv")
    validate_coverage(bars, sessions)
    _check_coverage_metadata(manifest, bars, sessions)
    if scan_privacy(directory, _archive_depth=1):
        raise ValueError("cached dataset privacy check failed")


def stage_release(*, release_dir: Path, expected_manifest_sha256: str,
                  expected_dataset_id: str, cache_dir: Path,
                  allow_synthetic: bool = False) -> Path:
    """Stage one immutable, caller-pinned version; do not change any active pointer."""
    for value in (expected_manifest_sha256, expected_dataset_id):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("manifest hash and dataset pin must be SHA-256 hex strings")
    release_dir = release_dir.resolve()
    if any(path.is_symlink() or not path.is_file() for path in release_dir.iterdir()):
        raise ValueError("downloaded release files must be regular files")
    manifest_path = release_dir / "release-manifest.json"
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("release manifest exceeds 1 MiB")
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError("release manifest hash does not match the caller pin")
    header = json.loads(raw)
    release_id = header.get("release_id")
    if not isinstance(release_id, str) or not re.fullmatch(r"data-[0-9]{4}-[0-9]{2}-[0-9]{2}-[A-Za-z0-9_-]+-r[1-9][0-9]*", release_id):
        raise ValueError("unsafe release identity")
    items = header.get("datasets")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("release must contain exactly one dataset")
    asset = items[0].get("asset")
    if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz", asset):
        raise ValueError("unsafe release asset")
    required = {"release-manifest.json", "quality-report.json", "SHA256SUMS", asset}
    if {path.name for path in release_dir.iterdir()} != required:
        raise ValueError("downloaded release has unexpected files")
    for path in release_dir.iterdir():
        limit = 50 * 1024 * 1024 if path.name == asset else 1024 * 1024
        if path.stat().st_size > limit:
            raise ValueError("downloaded release exceeds size limits")

    # Actions artifact downloads drop their outer directory. Normalize only inside staging.
    with tempfile.TemporaryDirectory(prefix="consume-release-") as temp:
        normalized = Path(temp) / release_id
        shutil.copytree(release_dir, normalized)
        if _sha256(normalized / "release-manifest.json") != expected_manifest_sha256:
            raise ValueError("copied release manifest hash differs from pin")
        release = verify_release(normalized)
        item = release["datasets"][0]
        if item["dataset_id"] != expected_dataset_id:
            raise ValueError("release dataset does not match the caller pin")
        if release["data_class"] == "synthetic_fixture" and not allow_synthetic:
            raise ValueError("synthetic data is allowed only for explicit smoke tests")
        unpacked = Path(temp) / "unpacked"
        _safe_extract(normalized / item["asset"], unpacked)
        source = unpacked / item["catalog_dataset"]
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("market") != "CN" or manifest.get("currency") != "CNY"
                or manifest.get("series_type") not in {
                    "price_index", "total_return_index", "etf_raw",
                }
                or manifest.get("timezone") != "Asia/Shanghai" or manifest.get("frequency") != "1d"
                or manifest.get("adjust") not in {"", "raw", "none"}):
            raise ValueError("dataset is incompatible with the domestic legacy reader")
        manifest.update(
            market="A", upstream_dataset_id=expected_dataset_id,
            upstream_release_id=release_id,
            upstream_release_manifest_sha256=expected_manifest_sha256,
            consumer_adapter_version=ADAPTER,
            calendar_sha256=_sha256(source / "calendar.csv"),
        )
        manifest["dataset_id"] = dataset_identity(manifest)
        cache_dir = cache_dir.resolve()
        target = cache_dir / release_id / expected_dataset_id / ADAPTER
        if any(path.is_symlink() for path in (target, target.parent, target.parent.parent)):
            raise ValueError("cache version paths cannot be symlinks")
        if target.exists():
            _check_export(target, expected_manifest_sha256, expected_dataset_id, manifest["dataset_id"])
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=target.parent) as staging_dir:
            staging = Path(staging_dir) / "dataset"
            staging.mkdir()
            for name in ("bars.csv", "calendar.csv"):
                shutil.copyfile(source / name, staging / name)
            (staging / "manifest.json").write_bytes(_json_bytes(manifest))
            _check_export(staging, expected_manifest_sha256, expected_dataset_id, manifest["dataset_id"])
            try:
                os.rename(staging, target)
            except OSError:
                if not target.exists():
                    raise
                _check_export(target, expected_manifest_sha256, expected_dataset_id, manifest["dataset_id"])
        return target
