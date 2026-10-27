"""Scan actual Git objects, including staged bytes and newly introduced commits."""
from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
import re
import subprocess

from .privacy import MAX_TEXT_BYTES, scan_bytes, text_findings
from .provenance import REPOSITORY_FILES


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('privacy check cannot read Git objects') from exc


def load_private_terms(path: Path | None, root: Path) -> tuple[str, ...]:
    if path is None:
        return ()
    if path.resolve().is_relative_to(root.resolve()):
        raise ValueError('private terms file must be outside the repository')
    try:
        if path.stat().st_size > 65536:
            raise ValueError('private terms file exceeds limit')
        terms = tuple(line.strip() for line in path.read_text(encoding='utf-8').splitlines()
                      if line.strip() and not line.lstrip().startswith('#'))
    except (OSError, UnicodeError) as exc:
        raise ValueError('private terms file cannot be read') from exc
    if not terms or len(terms) > 256 or any(len(term) < 3 for term in terms):
        raise ValueError('private terms file must contain 1 to 256 terms of at least 3 characters')
    return terms


def _scan_entry(root, mode, oid, name, private_terms):
    # Do not echo a path rejected by the publication allowlist.
    if not any(fnmatchcase(name, pattern) for pattern in REPOSITORY_FILES):
        return ['Git file outside publication allowlist']
    if mode not in {'100644', '100755'}:
        return ['Git symlink, submodule or unmerged entry is not allowed']
    if int(_git(root, 'cat-file', '-s', oid)) > MAX_TEXT_BYTES:
        return ['Git blob exceeds privacy scan limit']
    data = _git(root, 'cat-file', 'blob', oid)
    return scan_bytes(name, data, private_terms=private_terms)


def scan_index(root: Path, *, private_terms: tuple[str, ...] = ()) -> list[str]:
    findings = []
    raw = _git(root, 'ls-files', '--stage', '-z').decode('utf-8')
    for record in filter(None, raw.split('\0')):
        metadata, name = record.split('\t', 1)
        mode, oid, stage = metadata.split()
        if stage != '0':
            findings.append('Git index has unresolved conflicts')
            continue
        findings.extend(_scan_entry(root, mode, oid, name, private_terms))
    return findings


def scan_worktree(root: Path, *, private_terms: tuple[str, ...] = ()) -> list[str]:
    """Inspect publication candidates, including untracked files, not ignored caches."""
    findings = []
    names = _git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').decode('utf-8')
    for name in sorted(set(filter(None, names.split('\0')))):
        if not any(fnmatchcase(name, pattern) for pattern in REPOSITORY_FILES):
            findings.append('Git file outside publication allowlist')
            continue
        path = root / name
        if path.is_symlink():
            findings.append('Git worktree symlink is not allowed')
        elif path.is_file():
            if path.stat().st_size > MAX_TEXT_BYTES:
                findings.append('Git file exceeds privacy scan limit')
            else:
                findings.extend(scan_bytes(name, path.read_bytes(), private_terms=private_terms))
    return findings


def scan_commits(root: Path, revision_range: str, *, private_terms: tuple[str, ...] = ()) -> list[str]:
    refs = revision_range.split('..')
    if len(refs) not in (1, 2) or any(not re.fullmatch(r'(?:HEAD|[0-9a-f]{7,40})', ref) for ref in refs):
        raise ValueError('privacy revision range must use commit hashes or HEAD')
    resolved = [_git(root, 'rev-parse', '--verify', ref + '^{commit}').decode().strip() for ref in refs]
    revisions = [resolved[-1]] + (['^' + resolved[0]] if len(resolved) == 2 else [])
    commits = _git(root, 'rev-list', '--max-count=1001', *revisions).decode().splitlines()
    if len(commits) > 1000:
        raise ValueError('privacy commit range exceeds 1000 commits; inspect smaller ranges')
    findings = []
    seen = set()
    for commit in commits:
        label = 'commit ' + commit[:12]
        metadata = _git(root, 'show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce%x00%B', commit).decode('utf-8')
        author, author_email, committer, committer_email, message = metadata.split('\0', 4)
        for email in (author_email, committer_email):
            if any(term.casefold() in email.casefold() for term in private_terms):
                findings.append(f'{label}: local private term in commit identity')
            if not (re.fullmatch(r'[A-Za-z0-9+._-]+@users\.noreply\.github\.com', email)
                    or email == 'noreply' + '@github.com' or email.endswith('@example.invalid')):
                findings.append(f'{label}: non-public commit identity')
        findings.extend(text_findings('\n'.join((author, committer, message)), label, private_terms=private_terms))
        for record in filter(None, _git(root, 'ls-tree', '-r', '-z', commit).decode('utf-8').split('\0')):
            metadata, name = record.split('\t', 1)
            mode, _, oid = metadata.split()
            key = (mode, oid, name)
            if key in seen:
                continue
            seen.add(key)
            findings.extend(f'{label}: {finding}' for finding in
                            _scan_entry(root, mode, oid, name, private_terms))
    return findings
