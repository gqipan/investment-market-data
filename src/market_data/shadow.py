"""Read-only comparisons of pinned domestic research datasets; no promotion."""

from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re

from .consumer import _check_export
from .quality import read_bars


def _load(directory: Path, pin: str):
    if not re.fullmatch(r'[0-9a-f]{64}', pin):
        raise ValueError('consumer manifest pin must be SHA-256')
    path = directory / 'manifest.json'
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('unsafe consumer manifest')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError('consumer manifest does not match the caller pin')
    m = json.loads(raw)
    if not isinstance(m, dict):
        raise ValueError('consumer manifest must be an object')
    for key in ('symbol', 'market', 'currency', 'series_type', 'frequency', 'timezone',
                'volume_unit', 'adapter_version', 'consumer_adapter_version', 'upstream_release_id'):
        if not isinstance(m.get(key), str) or not m[key]:
            raise ValueError('missing consumer metadata: ' + key)
    for key in ('upstream_release_manifest_sha256', 'upstream_dataset_id', 'dataset_id', 'sha256', 'calendar_sha256'):
        if not isinstance(m.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', m[key]):
            raise ValueError('invalid consumer identity: ' + key)
    if m.get('synthetic') is not False or m.get('data_class') != 'normalized_price':
        raise ValueError('synthetic or non-price data cannot be shadow input')
    for name in ('bars.csv', 'calendar.csv'):
        p = directory / name
        if p.is_symlink() or p.stat().st_size > 50 * 1024 * 1024:
            raise ValueError('unsafe consumer data file')
    _check_export(directory, m['upstream_release_manifest_sha256'], m['upstream_dataset_id'], m['dataset_id'])
    if (m.get('market') != 'A' or m.get('currency') != 'CNY'
            or not re.fullmatch(r'[0-9]{6}', m['symbol'])
            or m.get('series_type') not in {'price_index', 'total_return_index'}
            or m.get('frequency') != '1d'
            or m.get('timezone') != 'Asia/Shanghai' or m.get('adjust') not in ('', 'raw', 'none')):
        raise ValueError('shadow input must be domestic raw daily price-index data')
    return m, {b.day:b for b in read_bars(directory / 'bars.csv')}


def compare_shadow(*, candidate: Path, baseline: Path, candidate_manifest_sha256: str,
                   baseline_manifest_sha256: str, expected_session: str,
                   minimum_sessions: int = 900, relative_tolerance: float = 0) -> dict:
    """Return one comparison, never evidence of five-day acceptance or permission to switch."""
    expected = date.fromisoformat(expected_session)
    if minimum_sessions < 1 or not math.isfinite(relative_tolerance) or relative_tolerance < 0:
        raise ValueError('invalid history requirement or relative tolerance')
    cm, cb = _load(candidate, candidate_manifest_sha256)
    bm, bb = _load(baseline, baseline_manifest_sha256)
    for key in ('symbol', 'market', 'currency', 'series_type', 'frequency', 'timezone', 'volume_unit'):
        if cm.get(key) != bm.get(key):
            raise ValueError('incompatible shadow metadata: ' + key)
    overlap = sorted(cb.keys() & bb.keys())
    differences = []
    max_error = 0.0
    for day in overlap:
        different = False
        for field in ('open', 'high', 'low', 'close', 'volume'):
            actual, reference = getattr(cb[day], field), getattr(bb[day], field)
            error = abs(actual - reference) / max(abs(reference), 1e-300)
            max_error = max(max_error, error)
            different |= error > relative_tolerance
        if different:
            differences.append(day.isoformat())
    reasons = []
    if min(len(cb), len(bb), len(overlap)) < minimum_sessions:
        reasons.append('insufficient_history')
    if max(cb) != expected or max(bb) != expected:
        reasons.append('unexpected_last_session')
    # Histories may start at different times; holes within their shared date window may not.
    shared_start, shared_end = max(min(cb), min(bb)), min(max(cb), max(bb))
    missing_candidate = sorted(d for d in bb if shared_start <= d <= shared_end and d not in cb)
    missing_baseline = sorted(d for d in cb if shared_start <= d <= shared_end and d not in bb)
    if missing_candidate or missing_baseline or not overlap:
        reasons.append('session_mismatch')
    if differences:
        reasons.append('numeric_difference')
    return {'symbol':cm['symbol'], 'expected_session':expected_session,
        'candidate_dataset_id':cm['dataset_id'], 'baseline_dataset_id':bm['dataset_id'],
        'same_dataset':cm['dataset_id'] == bm['dataset_id'],
        'candidate_sessions':len(cb), 'baseline_sessions':len(bb), 'overlap_sessions':len(overlap),
        'candidate_last_session':max(cb).isoformat(), 'baseline_last_session':max(bb).isoformat(),
        'minimum_sessions':minimum_sessions, 'relative_tolerance':relative_tolerance,
        'different_sessions':len(differences), 'difference_dates':differences,
        'maximum_relative_error':max_error if math.isfinite(max_error) else None,
        'missing_candidate_dates':[str(d) for d in missing_candidate],
        'missing_baseline_dates':[str(d) for d in missing_baseline],
        'comparison_passed':not reasons, 'reasons':reasons, 'promotion_allowed':False}
