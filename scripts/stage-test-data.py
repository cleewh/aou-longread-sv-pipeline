#!/usr/bin/env python3
# Task 12.3: CLI wrapper around scripts/stage_test_data.upload.
"""Stage the HealthOmics E2E test fixtures into an ap-southeast-1 S3 bucket.

Requirements: 15.1, 15.7
Design: §Test harness (``test/e2e/inputs.json`` shape), §Data Models.

Reads ``test/e2e/inputs.json`` and iterates every object in the
``staged_inputs`` and ``truth_set`` blocks, calling
:func:`stage_test_data.upload.stage_object` on each. Prints a JSON summary
of per-entry statuses on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the ``stage_test_data`` package importable despite the hyphen in
# *this* script's filename. The package lives at ``scripts/stage_test_data/``,
# so adding ``scripts/`` to sys.path suffices.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from stage_test_data.upload import (  # noqa: E402
    ChecksumMismatchError,
    UpstreamUnreachableError,
    stage_object,
)


DEFAULT_REGION = None  # Uses AWS CLI configured region
_REPO_ROOT = _SCRIPTS_DIR.parent
_DEFAULT_INPUTS_JSON = _REPO_ROOT / "test" / "e2e" / "inputs.json"


def _entries_from(inputs: dict) -> list[dict]:
    """Flatten ``staged_inputs`` + ``truth_set`` into one list of stage entries."""
    entries: list[dict] = []
    staged = inputs.get("staged_inputs") or {}
    if isinstance(staged, dict):
        for val in staged.values():
            if _looks_like_entry(val):
                entries.append(val)
    elif isinstance(staged, list):
        entries.extend(v for v in staged if _looks_like_entry(v))

    truth = inputs.get("truth_set") or {}
    if isinstance(truth, dict):
        for val in truth.values():
            if _looks_like_entry(val):
                entries.append(val)
    return entries


def _looks_like_entry(val) -> bool:
    return (
        isinstance(val, dict)
        and "s3_uri" in val
        and "sha256" in val
        and "size_bytes" in val
        and "upstream_uri" in val
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stage-test-data.py",
        description=(
            "Stage the HealthOmics E2E test fixtures into an "
            "ap-southeast-1 S3 bucket. Idempotent: objects already present "
            "with matching size + SHA-256 are skipped."
        ),
    )
    p.add_argument(
        "--bucket",
        required=True,
        help="Target S3 bucket name (must be in ap-southeast-1).",
    )
    p.add_argument(
        "--inputs-json",
        type=Path,
        default=_DEFAULT_INPUTS_JSON,
        help="Path to test/e2e/inputs.json (default: repo path).",
    )
    p.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region for the boto3 clients (default {DEFAULT_REGION}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + report the stage plan without touching S3 or the network.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        inputs = json.loads(args.inputs_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            f"stage-test-data.py: inputs JSON not found at {args.inputs_json}",
            file=sys.stderr,
        )
        return 2

    entries = _entries_from(inputs)
    summary: dict[str, object] = {
        "bucket": args.bucket,
        "inputs_json": str(args.inputs_json),
        "entry_count": len(entries),
        "dry_run": args.dry_run,
        "entries": [],
    }

    if args.dry_run:
        summary["entries"] = [
            {"status": "plan", "s3_uri": e["s3_uri"], "upstream_uri": e["upstream_uri"]}
            for e in entries
        ]
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - boto3 is a dev dep
        print(f"stage-test-data.py: boto3 is required ({exc})", file=sys.stderr)
        return 2

    s3 = boto3.client("s3", region_name=args.region)
    per_entry: list[dict] = []
    exit_code = 0
    for entry in entries:
        try:
            result = stage_object(entry, args.bucket, s3)
            per_entry.append({**result, "s3_uri": entry["s3_uri"]})
        except UpstreamUnreachableError as exc:
            per_entry.append(
                {
                    "status": "upstream_unreachable",
                    "s3_uri": entry["s3_uri"],
                    "upstream_uri": entry["upstream_uri"],
                    "error": str(exc),
                }
            )
            exit_code = 3
        except ChecksumMismatchError as exc:
            per_entry.append(
                {
                    "status": "checksum_mismatch",
                    "s3_uri": entry["s3_uri"],
                    "error": str(exc),
                }
            )
            exit_code = 4

    summary["entries"] = per_entry
    print(json.dumps(summary, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
