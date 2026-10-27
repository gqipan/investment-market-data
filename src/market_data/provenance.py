"""Repository checks used by the release gate, independent of data permission."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
import re
import subprocess


REPOSITORY_FILES = (
    "README.md", "LICENSE", "DATA_POLICY.md", "AGENTS.md", ".gitignore", "pyproject.toml",
    "src/market_data/*.py", "tests/test_*.py", "catalog/*.json",
    "fixtures/csi300-demo-*.csv", "docs/*.md", "docs/**/*.md",
    ".github/workflows/*.yml",
)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], check=True,
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("release is blocked: Git provenance cannot be verified") from exc
    return result.stdout.strip()


def check_repository_files(root: Path) -> None:
    names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for name in names.split("\0"):
        if name and not any(fnmatchcase(name, pattern) for pattern in REPOSITORY_FILES):
            raise ValueError("repository file is outside the publication allowlist")
        if name and (root / name).is_symlink():
            raise ValueError("repository symlink is not allowed")


def check_code_commit(root: Path, commit: str) -> None:
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release is blocked: code commit is not verified")
    if _git(root, "rev-parse", "--show-toplevel") != str(root.resolve()):
        raise ValueError("release is blocked: root is not the repository root")
    if _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}") != commit:
        raise ValueError("release is blocked: code commit is not verified")
    _git(root, "merge-base", "--is-ancestor", commit, "HEAD")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("release is blocked: working tree is not clean")
    if _git(root, "diff", commit, "HEAD", "--", "src", "pyproject.toml", ".github"):
        raise ValueError("release is blocked: collection code differs from recorded commit")
    check_repository_files(root)
