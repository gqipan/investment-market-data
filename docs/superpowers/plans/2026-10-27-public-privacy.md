# Public Privacy Implementation Plan

> Execute inline with `executing-plans`; use test-driven changes and verification before completion.

**Goal:** Prevent accidental disclosure through source, Git metadata, and published assets.

**Architecture:** Extend the shared scanner, add an index/commit inspection entry point, and gate an explicit attachment directory before upload. Reuse existing Agent guidance; keep real private identifiers outside the repository.

**Tech Stack:** Python 3.11 standard library, unittest, Git, GitHub Actions.

## Constraints

- Do not change source permissions or data coverage rules.
- Do not rewrite history, move tags, delete releases, or publish private evidence.
- Findings report locations and categories without matching secret values.
- Private deny terms are optional local files outside the repository, never public fixtures.

## Tasks

- [x] Scanner: add synthetic regression cases for keys, headers, URL credentials, contact fields, filenames, archive identities and local deny terms; run them failing, implement, then rerun. Files: `privacy.py`, `test_privacy.py`.
- [x] Git gate: test real temporary repositories with unsafe staged blobs, safe worktree replacements, commit identities/messages and intermediate commits. Implement `privacy_git.py` and CLI flags; wire newly introduced commits into CI without blocking on old history.
- [x] Publication: test final payload preparation rejects private reports and unknown files, strips archive identities and checks all staged attachments. Implement `publication.py`; replace shell tar/upload paths with the verified attachment directory and test before network writes.
- [x] Rules: reuse existing `AGENTS.md`, allow it in provenance, expand ignore patterns, document local/public checks and incident response. Keep historical impact evidence outside the public repository.
- [x] Verification: run full unittest suite, repository/index/privacy/catalog checks, synthetic release smoke, inspect diff and document limits. Leave a reviewable local change; no history rewriting.

## Verification outcome

172 unit tests pass, including staged/intermediate Git objects, metadata, deny terms, archive identity and cumulative archive roundtrip. Repository privacy, catalog and diff checks pass. YAML and bash syntax checks pass for all three workflows. The full candidate tree passes staged and introduced-commit checks in a temporary Git repository; synthetic release/consumer and catalog artifact smoke checks pass. No remote CI run or publication is claimed; history rewriting and remote deletion remain outside this implementation.
