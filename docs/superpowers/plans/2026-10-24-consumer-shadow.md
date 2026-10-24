# Fixed release consumer implementation plan

**Goal:** Download pinned public releases and produce a read-only shadow comparison without enabling production switching.

**Architecture:** A bounded anonymous GitHub downloader delegates content verification and immutable cache installation to stage_release. A separate shadow checker verifies pinned consumer manifests and compares compatible raw daily series.

**Tech stack:** Python 3.11 standard library and unittest.

## Constraints

No private files, credentials, database access, pointer updates, source permission changes, automatic provider fallback or real-data publication. Synthetic packages are rejected except an explicit downloader smoke-test option. Shadow reports never authorize promotion.

## Tasks

- [x] Add tests/test_transport.py: exact fixed-tag success, wrong pins, draft/mutable releases, unexpected/cross-repo assets, oversized/truncated downloads, missing assets, synthetic default rejection and prior-cache preservation.
- [x] Observe failures, then implement src/market_data/transport.py with fetch_release(repository, release_id, expected_manifest_sha256, expected_dataset_id, cache_dir), bounded read_https and restricted redirects; invoke stage_release only after complete verified download.
- [x] Add tests/test_shadow.py: matched real cache, six-session history rejection, mismatched metadata, damaged pins/content, stale dates, missing overlap and numeric differences. Observe failures, then implement compare_shadow(candidate, baseline, candidate_manifest_sha256, baseline_manifest_sha256, expected_session, minimum_sessions=900, relative_tolerance=0).
- [x] Extend CLI with fetch-release and shadow-check; shadow emits JSON to stdout, nonpassing comparison exits 1 and malformed input exits 2. Update consumer-integration.md with exact commands and remaining deployment gates.
- [ ] Run full unittest, privacy/catalog checks, CLI smoke tests and independent review; push branch, create PR and wait for GitHub checks. Record the separately started main workflow run.

No integration into production in this branch. Existing scheduled check is started independently of feature implementation.
