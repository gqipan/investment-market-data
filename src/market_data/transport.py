"""Bounded anonymous downloads of caller-pinned, immutable public releases."""

import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .consumer import stage_release
from .general_release import stage_snapshot_release

METADATA_LIMIT = 1024 * 1024
ASSET_LIMIT = 50 * 1024 * 1024
HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


def validate_download_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment):
        raise ValueError('download destination must be trusted GitHub HTTPS')


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_https(url: str, limit: int) -> bytes:
    validate_download_url(url)
    try:
        accept = (
            'application/vnd.github+json'
            if urlsplit(url).hostname == 'api.github.com'
            else 'application/octet-stream'
        )
        request = Request(url, headers={
            'User-Agent': 'investment-market-data-consumer',
            'Accept': accept,
        })
        with build_opener(_Redirects()).open(request, timeout=20) as response:
            validate_download_url(response.geturl())
            if int(response.headers.get('Content-Length', '0')) > limit:
                raise ValueError('download exceeds size limit')
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('download exceeds size limit')
        return raw
    except (URLError, OSError) as exc:
        # Signed redirect URLs must never appear in error logs.
        raise ValueError('GitHub download failed; keep the previous version') from exc


def fetch_release(*, repository: str, release_id: str, expected_manifest_sha256: str,
                  expected_dataset_id: str, cache_dir: Path, allow_synthetic: bool = False,
                  download=read_https) -> Path:
    """Install a fixed public version; never change a pointer or request credentials."""
    with tempfile.TemporaryDirectory(prefix='market-release-download-') as temp:
        directory = _download_release(
            repository=repository,
            release_id=release_id,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_dataset_id=expected_dataset_id,
            destination=Path(temp) / release_id,
            allow_synthetic=allow_synthetic,
            download=download,
        )
        return stage_release(release_dir=directory,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_dataset_id=expected_dataset_id,
            cache_dir=cache_dir,
            allow_synthetic=allow_synthetic)


def fetch_snapshot_release(*, repository: str, release_id: str,
                           expected_manifest_sha256: str,
                           expected_dataset_id: str,
                           expected_data_class: str,
                           cache_dir: Path, download=read_https) -> Path:
    """Download and install one pinned close or valuation release."""
    with tempfile.TemporaryDirectory(prefix='market-snapshot-download-') as temp:
        directory = _download_release(
            repository=repository,
            release_id=release_id,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_dataset_id=expected_dataset_id,
            destination=Path(temp) / release_id,
            allow_synthetic=False,
            download=download,
        )
        return stage_snapshot_release(
            release_dir=directory,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_dataset_id=expected_dataset_id,
            expected_data_class=expected_data_class,
            cache_dir=cache_dir,
        )


def _download_release(*, repository: str, release_id: str,
                      expected_manifest_sha256: str, expected_dataset_id: str,
                      destination: Path,
                      allow_synthetic: bool, download) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*', repository):
        raise ValueError('invalid GitHub repository')
    if not re.fullmatch(r'data-\d{4}-\d{2}-\d{2}-[A-Za-z0-9_-]+-r[1-9]\d*', release_id):
        raise ValueError('a fixed data release tag is required')
    for pin in (expected_manifest_sha256, expected_dataset_id):
        if not re.fullmatch(r'[0-9a-f]{64}', pin):
            raise ValueError('manifest and dataset pins must be SHA-256')

    def bounded(url, limit):
        raw = download(url, limit)
        if not isinstance(raw, bytes) or len(raw) > limit or not raw:
            raise ValueError('empty or oversized download')
        return raw

    metadata = json.loads(bounded(f'https://api.github.com/repos/{repository}/releases/tags/{release_id}', METADATA_LIMIT))
    if not isinstance(metadata, dict):
        raise ValueError('invalid GitHub release metadata')
    if (metadata.get('tag_name') != release_id or metadata.get('draft') is not False
            or metadata.get('prerelease') is not False or metadata.get('immutable') is not True):
        raise ValueError('release must be published, immutable, and match the fixed tag')
    assets = metadata.get('assets')
    if not isinstance(assets, list) or len(assets) != 4 or any(not isinstance(a, dict) for a in assets):
        raise ValueError('release must contain exactly four package assets')
    by_name = {a.get('name'): a for a in assets if isinstance(a.get('name'), str)}
    if len(by_name) != 4 or not {'release-manifest.json', 'quality-report.json', 'SHA256SUMS'} <= by_name.keys():
        raise ValueError('missing or duplicate release assets')
    base = f'https://github.com/{repository}/releases/download/{release_id}/'
    for name, asset in by_name.items():
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name)
                or asset.get('browser_download_url') != base + name):
            raise ValueError('unexpected asset URL or name')
        size = asset.get('size')
        limit = ASSET_LIMIT if name.endswith('.tar.gz') else METADATA_LIMIT
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= limit:
            raise ValueError('asset size exceeds limits')

    def read_asset(name):
        limit = ASSET_LIMIT if name.endswith('.tar.gz') else METADATA_LIMIT
        raw = bounded(base + name, limit)
        if len(raw) != by_name[name]['size']:
            raise ValueError('truncated download or changed asset size')
        return raw

    raw = read_asset('release-manifest.json')
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError('release manifest does not match the caller pin')
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError('invalid release manifest')
    items = manifest.get('datasets')
    if (manifest.get('release_id') != release_id or not isinstance(items, list)
            or len(items) != 1 or not isinstance(items[0], dict)):
        raise ValueError('release identity or dataset list is invalid')
    asset_name = items[0].get('asset')
    if (not isinstance(asset_name, str) or not asset_name.endswith('.tar.gz')
            or set(by_name) != {'release-manifest.json', 'quality-report.json', 'SHA256SUMS', asset_name}):
        raise ValueError('unexpected package assets')
    if manifest.get('data_class') == 'synthetic_fixture':
        if not allow_synthetic:
            raise ValueError('synthetic data requires explicit smoke-test mode')
    elif manifest.get('publication_eligible') is not True:
        raise ValueError('release is not eligible for public consumption')
    destination.mkdir()
    (destination / 'release-manifest.json').write_bytes(raw)
    for name in sorted(set(by_name) - {'release-manifest.json'}):
        (destination / name).write_bytes(read_asset(name))
    return destination
