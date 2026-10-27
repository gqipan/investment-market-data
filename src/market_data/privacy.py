"""Fail-closed checks; findings never include matching text or unsafe filenames."""
from __future__ import annotations

import csv
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

SKIPPED_DIRS = {'.git', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache'}
TEXT_SUFFIXES = {'', '.csv', '.json', '.md', '.py', '.toml', '.txt', '.yml', '.yaml'}
MAX_TEXT_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
SECRET_PATTERNS = (
    re.compile(r'\bgh[pousr]_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b'),
    re.compile(r'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}'),
    re.compile(r'''(?i)\b(?:Cookie|Set-Cookie)\s*:\s*[^\s=;"']+=[^\s;"']{8,}'''),
    re.compile(r"""(?i)https?://[^\s/@:'"]+:[^\s/@'"]+@"""),
    re.compile(r'''(?i)["']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|client[_-]?secret)["']?\s*[:=]\s*["'][A-Za-z0-9_./+~=-]{8,}["']'''),
    re.compile(r'(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|client[_-]?secret)\s*[:=]\s*[A-Za-z0-9_+/=-]{16,}(?=\s|[,;}]|$)'),
)
PRIVATE_PATTERNS = (
    re.compile(r'/' + r'Users/[^/\s]+'),
    re.compile(r'[A-Za-z]:\\Users\\[^\\\s]+'),
    re.compile(r'/' + r'home/[^/\s]+'),
    re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?!invalid\b)[A-Za-z]{2,}\b'),
    re.compile(r'''(?i)["']?(?:phone|mobile|contact_name|personal_address|身份证|手机号|联系电话)["']?\s*[:=]\s*["']?[^\s"',}]{2,}'''),
)
PHONE_IN_NAME = re.compile(r'(?<![A-Za-z0-9])1[3-9]\d{9}(?![A-Za-z0-9])')
PRIVATE_FIELDS = {
    'account', 'account_id', 'account_number', 'holdings', 'cash', 'positions',
    'transactions', 'portfolio_transactions', 'net_asset_value', 'user_id',
    'phone', 'mobile', 'contact_name', 'personal_address', 'id_card',
    '账户', '账号', '持仓', '现金', '交易记录', '资产净值', '手机号', '身份证',
}


def _private_keys(value) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in PRIVATE_FIELDS or _private_keys(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_private_keys(item) for item in value)
    return False


def text_findings(text: str, label: str, *, private_terms: tuple[str, ...] = ()) -> list[str]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(pattern.search(line) for pattern in PRIVATE_PATTERNS):
            findings.append(f'{label}:{number}: private path or identity')
        if any(pattern.search(line) for pattern in SECRET_PATTERNS):
            findings.append(f'{label}:{number}: possible credential')
        if any(term.casefold() in line.casefold() for term in private_terms):
            findings.append(f'{label}:{number}: local private term')
    return findings


def safe_label(name: str, private_terms: tuple[str, ...] = ()) -> str:
    if (text_findings(name, '', private_terms=private_terms) or PHONE_IN_NAME.search(name)
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        return '<redacted-path>'
    return name


def scan_bytes(name: str, data: bytes, *, private_terms: tuple[str, ...] = ()) -> list[str]:
    label = safe_label(name, private_terms)
    findings = [f'{label}: private filename'] if label != name else []
    path = PurePosixPath(name)
    if (path.suffix.lower() not in TEXT_SUFFIXES or path.name.startswith('.env')
            or any(part in {'.ssh', '.aws', '.gnupg', '.credentials'} for part in path.parts)):
        return findings + [f'{label}: unsupported or private file type']
    if len(data) > MAX_TEXT_BYTES:
        return findings + [f'{label}: text file exceeds scan limit']
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return findings + [f'{label}: unreadable text file']
    if path.suffix.lower() == '.csv':
        header = next(csv.reader(io.StringIO(text)), [])
        if any(field.strip().lower() in PRIVATE_FIELDS for field in header):
            findings.append(f'{label}: private account or contact fields')
    if path.suffix.lower() == '.json':
        try:
            if _private_keys(json.loads(text)):
                findings.append(f'{label}: private account or contact fields')
        except json.JSONDecodeError:
            findings.append(f'{label}: invalid JSON cannot be privacy checked')
    return findings + text_findings(text, label, private_terms=private_terms)


def scan_archive(fileobj, label: str, *, private_terms: tuple[str, ...] = ()) -> list[str]:
    """Inspect streaming members without extracting or trusting tar metadata."""
    findings = []
    total = 0
    seen = set()
    try:
        with tarfile.open(fileobj=fileobj, mode='r|gz') as archive:
            for count, member in enumerate(archive, 1):
                name = member.name
                member_label = safe_label(name, private_terms)
                if member_label != name:
                    findings.append(f'{label}!{member_label}: private filename')
                relative = PurePosixPath(name)
                if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                        or not (member.isfile() or member.isdir()) or name in seen):
                    return findings + [f'{label}: unsafe archive member']
                seen.add(name)
                total += member.size
                if count > 10_000 or total > MAX_ARCHIVE_BYTES or member.size > MAX_TEXT_BYTES:
                    return findings + [f'{label}: archive exceeds scan limit']
                if (member.uid or member.gid or member.uname or member.gname
                        or set(member.pax_headers) - {'path', 'size', 'mtime'}):
                    findings.append(f'{label}: private archive identity or metadata')
                if member.isdir():
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    return findings + [f'{label}: unreadable archive member']
                with stream:
                    data = stream.read(MAX_TEXT_BYTES + 1)
                if name.endswith('.tar.gz'):
                    findings.append(f'{label}: nested archive is not allowed')
                else:
                    findings.extend(f'{label}!{finding}' for finding in
                                    scan_bytes(name, data, private_terms=private_terms))
            if not seen:
                findings.append(f'{label}: empty archive')
    except (OSError, ValueError, tarfile.TarError):
        findings.append(f'{label}: archive could not be safely inspected')
    return findings


def _traversal_failed(error):
    raise ValueError('privacy traversal failed; directory could not be inspected') from error


def iter_text_files(root: Path, *, skip_infrastructure: bool = True):
    # Prune before descent; ignored virtual environments can contain millions of files.
    import os
    for current, dirs, files in os.walk(root, followlinks=False, onerror=_traversal_failed):
        for name in sorted(dirs[:]):
            path = Path(current) / name
            if path.is_symlink():
                yield path
                dirs.remove(name)
            elif skip_infrastructure and name in SKIPPED_DIRS:
                dirs.remove(name)
        for name in sorted(files):
            yield Path(current) / name


def scan_privacy(root: Path, *, _archive_depth: int = 0,
                 private_terms: tuple[str, ...] = ()) -> list[str]:
    if not root.is_dir() or root.is_symlink():
        return ['scan root is missing or linked']
    findings = []
    for path in iter_text_files(root, skip_infrastructure=not _archive_depth):
        name = path.relative_to(root).as_posix()
        label = safe_label(name, private_terms)
        if path.is_symlink():
            findings.append(f'{label}: symlink is not allowed')
            continue
        if path.name == '.DS_Store':
            continue
        if path.name.endswith('.tar.gz'):
            if _archive_depth:
                findings.append(f'{label}: nested archive is not allowed')
            elif path.stat().st_size > MAX_ARCHIVE_BYTES:
                findings.append(f'{label}: compressed archive exceeds scan limit')
            else:
                if label != name:
                    findings.append(f'{label}: private filename')
                with path.open('rb') as stream:
                    findings.extend(scan_archive(stream, label, private_terms=private_terms))
            continue
        if path.stat().st_size > MAX_TEXT_BYTES:
            findings.append(f'{label}: text file exceeds scan limit')
            continue
        findings.extend(scan_bytes(name, path.read_bytes(), private_terms=private_terms))
    return findings
