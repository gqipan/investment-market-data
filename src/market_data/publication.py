"""Build and inspect the exact daily attachment set before any remote write."""
from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil
import tempfile

from .cumulative import verify_cumulative
from .privacy import _traversal_failed, scan_privacy
from .release import _sha256, _write_reproducible_tar

PAYLOAD_FILES = {
    'manifest.json', 'bars.csv', 'calendar.csv', 'close.csv', 'raw-bars.json',
    'observations.csv', 'raw.json', 'coverage-report.json', 'coverage-report.md',
    'route-coverage.json', 'cumulative-manifest.json', 'registry.json',
}


def _copy_payload(source: Path, destination: Path, *, skip_request_cache: bool) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError('publication input must be a real directory')
    destination.mkdir()
    for current, dirs, files in os.walk(source, followlinks=False, onerror=_traversal_failed):
        current = Path(current)
        for name in sorted(dirs[:]):
            if skip_request_cache and current == source and name == 'request-cache':
                dirs.remove(name)
                continue
            path = current / name
            if path.is_symlink():
                raise ValueError('publication symlink is not allowed')
            (destination / path.relative_to(source)).mkdir()
        for name in sorted(files):
            path = current / name
            if path.is_symlink():
                raise ValueError('publication symlink is not allowed')
            if name not in PAYLOAD_FILES:
                raise ValueError('publication file is outside the payload allowlist')
            shutil.copyfile(path, destination / path.relative_to(source))


def prepare_publication(*, delta_dir: Path, cumulative_dir: Path, as_of: str,
                        output_dir: Path, private_terms: tuple[str, ...] = ()) -> Path:
    try:
        valid_date = date.fromisoformat(as_of).isoformat() == as_of
    except ValueError:
        valid_date = False
    if not valid_date:
        raise ValueError('publication date must be YYYY-MM-DD')
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError('publication output already exists')
    # Resolve only after rejecting symlinks, to avoid following an unsafe root.
    if cumulative_dir.is_symlink() or delta_dir.is_symlink():
        raise ValueError('publication symlink is not allowed')
    try:
        manifest = verify_cumulative(cumulative_dir)
    except (ValueError, OSError, KeyError, TypeError):
        # Inventory paths are untrusted and may themselves be private.
        raise ValueError('publication cumulative validation failed') from None
    if manifest.get('as_of') != as_of:
        raise ValueError('publication date differs from cumulative manifest')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    for source in (delta_dir, cumulative_dir):
        if output_dir.resolve().is_relative_to(source.resolve()):
            raise ValueError('publication output must be outside its inputs')
    with tempfile.TemporaryDirectory(prefix='publication-', dir=output_dir.parent) as temp:
        staging = Path(temp)
        delta = staging / 'delta'
        cumulative = staging / 'cumulative'
        _copy_payload(delta_dir, delta, skip_request_cache=True)
        _copy_payload(cumulative_dir, cumulative, skip_request_cache=False)
        for source in (delta, cumulative):
            if scan_privacy(source, _archive_depth=1, private_terms=private_terms):
                raise ValueError('publication input privacy check failed')
        for name in ('coverage-report.json', 'coverage-report.md'):
            if not (delta / name).is_file():
                raise ValueError('publication coverage reports are required')
        upload = staging / 'upload'
        upload.mkdir()
        archives = []
        for kind, source in (('delta', delta), ('cumulative', cumulative)):
            archive = upload / f'index-data-{kind}-{as_of}.tar.gz'
            _write_reproducible_tar(source, archive, '.')
            archives.append(archive)
        for name in ('coverage-report.json', 'coverage-report.md'):
            shutil.copyfile(delta / name, upload / name)
        (upload / 'SHA256SUMS').write_text(''.join(
            f'{_sha256(path)}  {path.name}\n' for path in sorted(archives)), encoding='utf-8')
        if scan_privacy(upload, private_terms=private_terms):
            raise ValueError('publication final attachment privacy check failed')
        # Directory is made visible only after every attachment passes.
        os.rename(upload, output_dir)
    return output_dir
