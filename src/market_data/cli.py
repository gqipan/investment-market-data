"""Command-line entry points for local validation and release candidates."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from .catalog import build_catalog_report, validate_catalog
from .privacy import scan_privacy
from .provenance import check_repository_files
from .release import assert_release_allowed, build_candidate, verify_release


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _detect_commit(root: Path) -> str:
    try:
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            check=True, capture_output=True, text=True,
        )
        if dirty.stdout.strip():
            return "local-uncommitted"
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "local-uncommitted"
    return result.stdout.strip() or "local-uncommitted"


def _release_schema(release_dir: Path) -> str:
    manifest = json.loads(
        (release_dir / "release-manifest.json").read_text(encoding="utf-8")
    )
    schema = manifest.get("schema_version")
    if not isinstance(schema, str):
        raise ValueError("release manifest has no schema version")
    return schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-data")
    subcommands = parser.add_subparsers(dest="command", required=True)

    privacy = subcommands.add_parser("privacy-check", help="scan text files for private data")
    privacy.add_argument("--path", type=Path, default=Path("."))
    privacy.add_argument("--repository", action="store_true", help="also enforce the Git file publication allowlist")

    collect = subcommands.add_parser("collect", help="collect a local raw index price snapshot")
    collect.add_argument("--root", type=Path, default=_default_root())
    collect.add_argument("--dataset", required=True)
    collect.add_argument("--provider", choices=("tencent_raw", "sina_raw", "akshare_eastmoney"))
    collect.add_argument("--start", required=True)
    collect.add_argument("--end", required=True)
    collect.add_argument("--output", required=True, type=Path)

    history = subcommands.add_parser("backfill", help="collect local price and valuation history; retain partial coverage")
    history.add_argument("--root", type=Path, default=_default_root())
    history.add_argument("--start", required=True)
    history.add_argument("--end", required=True)
    history.add_argument("--output", required=True, type=Path)
    history.add_argument("--include-csindex", action="store_true", help="explicit optional CSI API peg observations; keep separate methods")
    history.add_argument(
        "--allow-partial",
        action="store_true",
        help="publish a snapshot with explicit gaps only when coverage-report.json records them",
    )

    cumulative = subcommands.add_parser(
        "build-cumulative",
        help="append a verified daily batch to a cumulative release tree",
    )
    cumulative.add_argument("--delta-dir", required=True, type=Path)
    cumulative.add_argument("--output-dir", required=True, type=Path)
    cumulative.add_argument("--as-of", required=True)
    cumulative.add_argument("--previous-dir", type=Path)
    cumulative.add_argument("--previous-release")
    cumulative.add_argument("--previous-archive-sha256")

    materialize = subcommands.add_parser(
        "materialize-cumulative",
        help="merge ordered cumulative batches into current research snapshots",
    )
    materialize.add_argument("--root", required=True, type=Path)
    materialize.add_argument("--output", required=True, type=Path)

    registry_verify = subcommands.add_parser(
        "verify-registry-cache",
        help="verify every content-addressed dataset referenced by a local registry",
    )
    registry_verify.add_argument("--root", required=True, type=Path)
    registry_verify.add_argument("--registry", required=True, type=Path)

    delta_verify = subcommands.add_parser(
        "verify-delta-tree",
        help="verify every price, close, and valuation dataset in a collected batch",
    )
    delta_verify.add_argument("--root", required=True, type=Path)

    valuation = subcommands.add_parser("verify-valuation", help="verify a local independent valuation snapshot")
    valuation.add_argument("--snapshot", required=True, type=Path)

    stage = subcommands.add_parser("stage-release", help="verify pins and stage an immutable legacy-reader dataset")
    stage.add_argument("--release-dir", required=True, type=Path)
    stage.add_argument("--manifest-sha256", required=True)
    stage.add_argument("--dataset-id", required=True)
    stage.add_argument("--cache-dir", required=True, type=Path)
    stage.add_argument("--allow-synthetic", action="store_true", help="explicit smoke-test use only")

    stage_snapshot = subcommands.add_parser(
        "stage-snapshot-release",
        help="verify and install a pinned close-history or valuation release",
    )
    stage_snapshot.add_argument("--release-dir", required=True, type=Path)
    stage_snapshot.add_argument("--manifest-sha256", required=True)
    stage_snapshot.add_argument("--dataset-id", required=True)
    stage_snapshot.add_argument(
        "--data-class",
        required=True,
        choices=("normalized_close", "valuation_observation"),
    )
    stage_snapshot.add_argument("--cache-dir", required=True, type=Path)

    fetch = subcommands.add_parser("fetch-release", help="download a caller-pinned immutable public GitHub release")
    fetch.add_argument("--repository", required=True)
    fetch.add_argument("--release-id", required=True)
    fetch.add_argument("--manifest-sha256", required=True)
    fetch.add_argument("--dataset-id", required=True)
    fetch.add_argument("--cache-dir", required=True, type=Path)
    fetch.add_argument("--allow-synthetic", action="store_true", help="explicit smoke-test use only")

    fetch_snapshot = subcommands.add_parser(
        "fetch-snapshot-release",
        help="download a caller-pinned close-history or valuation GitHub release",
    )
    fetch_snapshot.add_argument("--repository", required=True)
    fetch_snapshot.add_argument("--release-id", required=True)
    fetch_snapshot.add_argument("--manifest-sha256", required=True)
    fetch_snapshot.add_argument("--dataset-id", required=True)
    fetch_snapshot.add_argument(
        "--data-class",
        required=True,
        choices=("normalized_close", "valuation_observation"),
    )
    fetch_snapshot.add_argument("--cache-dir", required=True, type=Path)

    shadow = subcommands.add_parser("shadow-check", help="compare pinned research caches without switching production")
    shadow.add_argument("--candidate", required=True, type=Path)
    shadow.add_argument("--baseline", required=True, type=Path)
    shadow.add_argument("--candidate-manifest-sha256", required=True)
    shadow.add_argument("--baseline-manifest-sha256", required=True)
    shadow.add_argument("--expected-session", required=True)
    shadow.add_argument("--minimum-sessions", type=int, default=900)
    shadow.add_argument("--relative-tolerance", type=float, default=0)

    catalog = subcommands.add_parser(
        "catalog-check", help="validate instrument, source, dataset, and watchlist metadata"
    )
    catalog.add_argument("--root", type=Path, default=_default_root())

    catalog_report = subcommands.add_parser(
        "catalog-report", help="write a human- and machine-readable catalog snapshot"
    )
    catalog_report.add_argument("--root", type=Path, default=_default_root())
    catalog_report.add_argument("--output-dir", type=Path, required=True)

    build = subcommands.add_parser(
        "build-candidate", help="build a deterministic local release candidate"
    )
    build.add_argument("--root", type=Path, default=_default_root())
    build.add_argument("--dataset", required=True)
    build.add_argument("--as-of", required=True)
    build.add_argument("--revision", type=int, default=1)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--generated-at")
    build.add_argument("--code-commit")
    build.add_argument("--snapshot", type=Path, help="verified local collection snapshot")

    build_snapshot = subcommands.add_parser(
        "build-snapshot-release",
        help="build a deterministic close-history or valuation release",
    )
    build_snapshot.add_argument("--root", type=Path, default=_default_root())
    build_snapshot.add_argument("--dataset", required=True)
    build_snapshot.add_argument("--snapshot", required=True, type=Path)
    build_snapshot.add_argument("--as-of", required=True)
    build_snapshot.add_argument("--revision", type=int, default=1)
    build_snapshot.add_argument("--output", required=True, type=Path)
    build_snapshot.add_argument("--generated-at")
    build_snapshot.add_argument("--code-commit")

    verify = subcommands.add_parser("verify-release", help="verify checksums and bundle contents")
    verify.add_argument("--release-dir", type=Path, required=True)

    release = subcommands.add_parser(
        "release-check", help="apply the private or public release gate"
    )
    release.add_argument("--root", type=Path, default=_default_root())
    release.add_argument("--release-dir", type=Path, required=True)
    release.add_argument(
        "--scope",
        choices=("private", "public"),
        default="private",
        help="target audience for the release",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "privacy-check":
        if args.repository:
            check_repository_files(args.path.resolve())
        findings = scan_privacy(args.path.resolve())
        if findings:
            print("\n".join(findings), file=sys.stderr)
            return 1
        print("privacy check passed")
        return 0
    if args.command == "collect":
        from .collection import collect_index
        output = collect_index(root=args.root, dataset_name=args.dataset, start=args.start,
            end=args.end, output=args.output, provider=args.provider)
        print(output)
        return 0
    if args.command == "backfill":
        from .history import backfill
        report = backfill(root=args.root, start=args.start, end=args.end, output=args.output, include_csindex=args.include_csindex,
            progress=lambda code, result: print(f"{code}: price={result['price']['status']}; missing daily valuation={','.join(result['missing_daily_history'])}", file=sys.stderr))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0 if report["status"] == "complete" or args.allow_partial else 1
    if args.command == "verify-valuation":
        from .valuation import verify_valuation
        print(json.dumps(verify_valuation(args.snapshot), ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    if args.command == "build-cumulative":
        from .cumulative import build_cumulative
        manifest = build_cumulative(
            delta_dir=args.delta_dir,
            output_dir=args.output_dir,
            as_of=args.as_of,
            previous_dir=args.previous_dir,
            previous_release=args.previous_release,
            previous_archive_sha256=args.previous_archive_sha256,
        )
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    if args.command == "materialize-cumulative":
        from .cumulative import materialize_cumulative
        result = materialize_cumulative(args.root, args.output)
        print(json.dumps(
            {
                "datasets": result["datasets"],
                "expected_session": result["expected_session"],
                "registry": str(result["registry"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        ))
        return 0
    if args.command == "verify-registry-cache":
        from .cumulative import verify_registry_cache
        counts = verify_registry_cache(args.root, args.registry)
        print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "verify-delta-tree":
        from .cumulative import verify_delta_tree
        counts = verify_delta_tree(args.root)
        print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "stage-release":
        from .consumer import stage_release
        output = stage_release(release_dir=args.release_dir,
            expected_manifest_sha256=args.manifest_sha256, expected_dataset_id=args.dataset_id,
            cache_dir=args.cache_dir, allow_synthetic=args.allow_synthetic)
        print(output)
        return 0
    if args.command == "stage-snapshot-release":
        from .general_release import stage_snapshot_release
        output = stage_snapshot_release(
            release_dir=args.release_dir,
            expected_manifest_sha256=args.manifest_sha256,
            expected_dataset_id=args.dataset_id,
            expected_data_class=args.data_class,
            cache_dir=args.cache_dir,
        )
        print(output)
        return 0
    if args.command == "fetch-release":
        from .transport import fetch_release
        print(fetch_release(repository=args.repository, release_id=args.release_id,
            expected_manifest_sha256=args.manifest_sha256, expected_dataset_id=args.dataset_id,
            cache_dir=args.cache_dir, allow_synthetic=args.allow_synthetic))
        return 0
    if args.command == "fetch-snapshot-release":
        from .transport import fetch_snapshot_release
        print(fetch_snapshot_release(
            repository=args.repository,
            release_id=args.release_id,
            expected_manifest_sha256=args.manifest_sha256,
            expected_dataset_id=args.dataset_id,
            expected_data_class=args.data_class,
            cache_dir=args.cache_dir,
        ))
        return 0
    if args.command == "shadow-check":
        from .shadow import compare_shadow
        report = compare_shadow(candidate=args.candidate, baseline=args.baseline,
            candidate_manifest_sha256=args.candidate_manifest_sha256,
            baseline_manifest_sha256=args.baseline_manifest_sha256,
            expected_session=args.expected_session, minimum_sessions=args.minimum_sessions,
            relative_tolerance=args.relative_tolerance)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0 if report["comparison_passed"] else 1
    if args.command == "catalog-check":
        counts = validate_catalog(args.root.resolve())
        print(
            "catalog check passed: "
            f"instruments={counts['instruments']} "
            f"sources={counts['sources']} "
            f"datasets={counts['datasets']} "
            f"tracked={counts['tracked']}"
        )
        return 0
    if args.command == "catalog-report":
        report = build_catalog_report(args.root.resolve())
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "catalog-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rows = [
            "# Selected index catalog report",
            "",
            f"Generated at: {report['generated_at']}",
            "",
            "> This artifact contains catalog metadata only. It does not contain market prices or valuation observations.",
            "",
            "| Code | Name | Price dataset | Price source | Valuation dataset | Valuation source |",
            "|---|---|---|---|---|---|",
        ]
        for item in report["tracked_indices"]:
            price = item["normalized_price"]
            valuation = item["valuation_observation"]
            rows.append(
                "| "
                f"{item['symbol']} | {item['display_name']} | "
                f"`{price['dataset_key']}` | `{price['source_id']}` / "
                f"{price['source_status']} | "
                f"`{valuation['dataset_key']}` | `{valuation['source_id']}` / "
                f"{valuation['source_status']} |"
            )
        (output_dir / "catalog-report.md").write_text(
            "\n".join(rows) + "\n",
            encoding="utf-8",
        )
        print(output_dir)
        return 0
    if args.command == "build-candidate":
        release_dir = build_candidate(
            root=args.root.resolve(),
            dataset_name=args.dataset,
            as_of=args.as_of,
            revision=args.revision,
            output_dir=args.output.resolve(),
            generated_at=args.generated_at,
            code_commit=args.code_commit or _detect_commit(args.root.resolve()),
            snapshot_dir=args.snapshot.resolve() if args.snapshot else None,
        )
        print(release_dir)
        return 0
    if args.command == "build-snapshot-release":
        from .general_release import build_snapshot_candidate
        release_dir = build_snapshot_candidate(
            root=args.root.resolve(),
            dataset_name=args.dataset,
            snapshot_dir=args.snapshot.resolve(),
            as_of=args.as_of,
            revision=args.revision,
            output_dir=args.output.resolve(),
            generated_at=args.generated_at,
            code_commit=args.code_commit or _detect_commit(args.root.resolve()),
        )
        print(release_dir)
        return 0
    if args.command == "verify-release":
        if _release_schema(args.release_dir.resolve()) == "1.3":
            from .general_release import verify_snapshot_release
            manifest = verify_snapshot_release(args.release_dir.resolve())
        else:
            manifest = verify_release(args.release_dir.resolve())
        print(f"verified {manifest['release_id']}")
        return 0
    if args.command == "release-check":
        if _release_schema(args.release_dir.resolve()) == "1.3":
            from .general_release import assert_snapshot_release_allowed
            assert_snapshot_release_allowed(
                args.root.resolve(), args.release_dir.resolve(), args.scope
            )
        else:
            assert_release_allowed(args.root.resolve(), args.release_dir.resolve(), args.scope)
        print(f"{args.scope} release gate passed")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except (FileExistsError, KeyError, ValueError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {message}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: file operation failed ({type(exc).__name__})", file=sys.stderr)
        return 2
