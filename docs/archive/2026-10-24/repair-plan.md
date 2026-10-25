# Data-source and release repair plan

> 历史记录：保留 2026-10-24 阶段的设计、约束与验收结果，不作为当前运行说明。后续来源许可、指数数量和发布方式已有变化；当前入口见[文档导航](../../README.md)。

Date: 2026-10-24

Goal: collect an auditable index price snapshot without requiring the index publisher's website, and reject unsafe release candidates.

The accepted review defines this work. Code remains suitable for public hosting; the public repository starts from a clean snapshot while the original history remains private. Local collection is separate from cloud collection and data redistribution approval. No personal application files or data are inputs.

- [x] Add regression tests for missed private files, invalid release provenance, permission revocation, and incorrect coverage metadata.
- [x] Scan generated content and reject unexpected files in release packages; check the repository's publishable file list separately.
- [x] Make release-check run package verification, privacy and repository checks, verify the Git commit and clean state, and consult current source permissions.
- [x] Add a bounded Tencent raw-index collector and an optional AkShare/Eastmoney adapter. Use explicit registered index symbols and an independently fetched trading calendar. Store snapshots outside Git; never silently switch providers.
- [x] Remove official-publisher defaults from price configuration, document source versus publisher and collection versus redistribution, and add a manual local collection entrypoint.
- [x] Run regression tests, CLI checks, a synthetic package round trip, and a live CSI 300 collection where network access permits. Record tested coverage and limitations.

Validation: 56 unit tests pass in a simulated GitHub Actions environment; catalog and repository privacy checks pass. Ten registered indices passed a six-session local Tencent check against the independent SZSE monthly calendar. The unsupported Tencent mapping for 930955 was removed and is rejected before networking. Real snapshots were not uploaded. AkShare/Eastmoney has offline coverage only in this network environment.

GitHub validation follow-up: build and stage a synthetic consumer contract in CI; add caller-pinned immutable consumer cache with explicit CN-to-A metadata conversion; recheck archive-directory privacy, catalog instrument binding and independent calendar permissions. The actual production downloader and active-version pointer remain consumer-side follow-up work.
