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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-data")
    subcommands = parser.add_subparsers(dest="command", required=True)

    privacy = subcommands.add_parser("privacy-check", help="scan text files for private data")
    privacy.add_argument("--path", type=Path, default=Path("."))
    privacy.add_argument("--repository", action="store_true", help="also enforce the Git file publication allowlist")

    collect = subcommands.add_parser("collect", help="collect a local raw index price snapshot")
    collect.add_argument("--root", type=Path, default=_default_root())
    collect.add_argument("--dataset", required=True)
    collect.add_argument("--provider", choices=("tencent_raw", "akshare_eastmoney"))
    collect.add_argument("--start", required=True)
    collect.add_argument("--end", required=True)
    collect.add_argument("--output", required=True, type=Path)

    stage = subcommands.add_parser("stage-release", help="verify pins and stage an immutable legacy-reader dataset")
    stage.add_argument("--release-dir", required=True, type=Path)
    stage.add_argument("--manifest-sha256", required=True)
    stage.add_argument("--dataset-id", required=True)
    stage.add_argument("--cache-dir", required=True, type=Path)
    stage.add_argument("--allow-synthetic", action="store_true", help="explicit smoke-test use only")

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
    if args.command == "stage-release":
        from .consumer import stage_release
        output = stage_release(release_dir=args.release_dir,
            expected_manifest_sha256=args.manifest_sha256, expected_dataset_id=args.dataset_id,
            cache_dir=args.cache_dir, allow_synthetic=args.allow_synthetic)
        print(output)
        return 0
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
    if args.command == "verify-release":
        manifest = verify_release(args.release_dir.resolve())
        print(f"verified {manifest['release_id']}")
        return 0
    if args.command == "release-check":
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
