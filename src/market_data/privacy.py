"""Fail-closed checks for private paths, identities, and common secrets."""

from __future__ import annotations

import re
import csv
import json
import tarfile
import tempfile
from pathlib import Path


SKIPPED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
TEXT_SUFFIXES = {
    "",
    ".csv",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
PRIVATE_PATTERNS = (
    re.compile(r"/" + r"Users/[^/\s]+"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    re.compile(r"/" + r"home/[^/\s]+"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?:com|cn|net|org)\b"),
)

PRIVATE_FIELDS = {
    "account", "account_id", "account_number", "holdings", "cash", "positions",
    "transactions", "portfolio_transactions", "net_asset_value", "user_id",
    "账户", "账号", "持仓", "现金", "交易记录", "资产净值",
}


def _private_keys(value) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in PRIVATE_FIELDS or _private_keys(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_private_keys(item) for item in value)
    return False


def iter_text_files(root: Path, *, skip_infrastructure: bool = True):
    for path in sorted(root.rglob("*")):
        if skip_infrastructure and any(part in SKIPPED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() or path.is_symlink():
            yield path


def scan_privacy(root: Path, *, _archive_depth: int = 0) -> list[str]:
    findings: list[str] = []
    for path in iter_text_files(root, skip_infrastructure=not _archive_depth):
        relative = path.relative_to(root)
        if path.is_symlink():
            findings.append(f"{relative}: symlink is not allowed")
            continue
        if path.name == ".DS_Store":
            continue
        if path.name.endswith(".tar.gz"):
            if _archive_depth:
                findings.append(f"{relative}: nested archive is not allowed")
                continue
            from .release import _safe_extract
            try:
                with tempfile.TemporaryDirectory(prefix="privacy-bundle-") as temp:
                    _safe_extract(path, Path(temp))
                    findings.extend(f"{relative}!{finding}" for finding in
                                    scan_privacy(Path(temp), _archive_depth=1))
            except (OSError, ValueError, tarfile.TarError):
                findings.append(f"{relative}: archive could not be safely inspected")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or path.name.startswith(".env"):
            findings.append(f"{relative}: unsupported or private file type")
            continue
        if path.stat().st_size > 50 * 1024 * 1024:
            findings.append(f"{relative}: text file exceeds scan limit")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"{relative}: unreadable text file")
            continue
        if path.suffix.lower() == ".csv":
            header = next(csv.reader(text.splitlines()), [])
            if any(field.strip().lower() in PRIVATE_FIELDS for field in header):
                findings.append(f"{relative}: private account or ledger fields")
        if path.suffix.lower() == ".json":
            try:
                if _private_keys(json.loads(text)):
                    findings.append(f"{relative}: private account or ledger fields")
            except json.JSONDecodeError:
                findings.append(f"{relative}: invalid JSON cannot be privacy checked")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern in PRIVATE_PATTERNS:
                if pattern.search(line):
                    findings.append(f"{relative}:{line_no}: private path or identity")
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(f"{relative}:{line_no}: possible credential")
    return findings
