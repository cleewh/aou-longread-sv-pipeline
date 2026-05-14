#!/usr/bin/env python3
# Task 10.9: HealthOmics submit-run CLI.
"""Submit an AoU long-read SV pipeline run on AWS HealthOmics (ap-southeast-1).

Requirements: 10.1, 10.2, 10.3, 10.4, 13.4, 17.2, 17.7, 17.8, 17.9, 17.12
Design: D10, D16, submit-run pseudocode.

The CLI is a thin composition of the pure-logic helpers under
:mod:`submit_run`:

1. Validate region residency of every S3 URI in the manifest (Property 9).
2. Validate that every container image URI is hosted in the ap-southeast-1
   ECR (Property 6 shape).
3. Resolve per-task resource overrides against :data:`TASK_DEFAULTS`
   (Property 11) — surfaced only as a printed report so operators can
   double-check the numbers before StartRun.
4. Plan chromosome shards from the reference FAI (Property 18).
5. Pick a cost-optimal instance for each task's resolved resource request
   (Property 15).
6. Finally, when not ``--dry-run``, call ``omics.start_run`` with the
   manifest as ``parameters`` and ``storageType`` from the manifest.

``--dry-run`` runs every pre-flight step, prints a JSON diagnostics report,
and exits ``0`` without touching HealthOmics — matching Design §Pre-flight
diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make the ``submit_run`` package importable despite the hyphen in *this*
# script's filename. The package lives at ``scripts/submit_run/`` so adding
# ``scripts/`` to sys.path is enough.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from submit_run.instance_selector import (  # noqa: E402
    InstanceType,
    load_price_list,
    select_instance,
)
from submit_run.residency import (  # noqa: E402
    EcrResidencyError,
    RegionResidencyError,
    check_ecr_residency,
    check_region_residency,
)
from submit_run.resources import TASK_DEFAULTS, resolve_task  # noqa: E402
from submit_run.shard_planner import plan_shards  # noqa: E402


DEFAULT_REGION = "ap-southeast-1"
_REPO_ROOT = _SCRIPTS_DIR.parent
_DEFAULT_CONTAINERS_MANIFEST = _REPO_ROOT / "containers" / "manifest.yaml"
_DEFAULT_PRICE_LIST = _REPO_ROOT / "pricing" / "healthomics-ap-southeast-1.json"


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="submit-run.py",
        description=(
            "Submit an AoU long-read SV pipeline run on AWS HealthOmics "
            "(ap-southeast-1) with region / ECR residency, resource, and "
            "shard pre-flight checks."
        ),
    )
    p.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Path to the Input_Manifest JSON to submit.",
    )
    p.add_argument(
        "--workflow-id",
        required=True,
        help="HealthOmics workflow ID (wfl-…) to StartRun against.",
    )
    p.add_argument(
        "--role-arn",
        required=True,
        help="IAM execution role ARN for HealthOmics to assume.",
    )
    p.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region (default {DEFAULT_REGION}); SDK region defaults here when unset.",
    )
    p.add_argument(
        "--containers-manifest",
        type=Path,
        default=_DEFAULT_CONTAINERS_MANIFEST,
        help="Path to containers/manifest.yaml (for ECR residency check).",
    )
    p.add_argument(
        "--price-list",
        type=Path,
        default=_DEFAULT_PRICE_LIST,
        help="Path to the HealthOmics ap-southeast-1 price list JSON.",
    )
    p.add_argument(
        "--fai",
        type=Path,
        default=None,
        help=(
            "Optional local path to the reference .fai. When supplied, the "
            "shard planner uses its contents; otherwise the shard plan is "
            "skipped in the dry-run report."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every pre-flight check, print the report as JSON, and exit without StartRun.",
    )
    return p


def _image_uris_from_manifest(
    containers_manifest_path: Path, account_id: str
) -> list[str]:
    """Build the list of canonical ap-southeast-1 ECR URIs from containers/manifest.yaml.

    The manifest stores one entry per tool with ``platforms`` and
    ``digest_<platform>`` fields. We build one URI per listed platform using
    the recorded digest (or a placeholder when the manifest has not yet been
    populated by ``mirror-images.py``; the ECR residency regex will still
    reject a bad hostname, so placeholders do not cause false passes).
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - yaml is a dev dep
        raise SystemExit(
            "submit-run.py: PyYAML is required to parse the containers manifest"
        ) from exc

    data = yaml.safe_load(containers_manifest_path.read_text(encoding="utf-8"))
    uris: list[str] = []
    for entry in data.get("images", []):
        repo = entry["ecr_repo"]
        platforms = entry.get("platforms", [])
        for plat in platforms:
            suffix = plat.split("/")[-1]
            digest = entry.get(f"digest_{suffix}") or "sha256:" + "0" * 64
            # Defensive: a fully-unfilled placeholder is a literal string, not a
            # sha256 hex digest. We substitute a zero-filled digest so the
            # residency regex has something valid to match against — this CLI
            # is about *residency*, not digest-pinning (Property 6 handles
            # pinning at the WDL lint step).
            if digest.startswith("sha256:") and len(digest) != len("sha256:") + 64:
                digest = "sha256:" + "0" * 64
            uris.append(
                f"{account_id}.dkr.ecr.ap-southeast-1.amazonaws.com/{repo}@{digest}"
            )
    return uris


def _resource_report(manifest: dict) -> dict[str, dict[str, int]]:
    """Resolve per-task resource requests from the manifest overrides."""
    report: dict[str, dict[str, int]] = {}
    for task, defaults in TASK_DEFAULTS.items():
        override: dict[str, int] = {}
        for field in defaults:
            key = f"{task}_{field}"
            value = manifest.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                override[field] = value
        report[task] = resolve_task(task, override)
    return report


def _instance_report(
    resource_report: dict[str, dict[str, int]], price_list: dict
) -> dict[str, dict[str, float | int | str]]:
    out: dict[str, dict[str, float | int | str]] = {}
    for task, res in resource_report.items():
        inst: InstanceType = select_instance(
            res["cpu"], res["memory_gb"], res["disk_gb"], price_list
        )
        out[task] = {
            "name": inst.name,
            "cpu": inst.cpu,
            "memory_gb": inst.memory_gb,
            "hourly_usd": inst.hourly_usd,
        }
    return out


def _shard_report(fai_path: Path | None, shard_by_chromosome: bool) -> dict | None:
    if fai_path is None:
        return None
    fai_contents = fai_path.read_text(encoding="utf-8")
    regions = plan_shards(fai_contents, shard_by_chromosome)
    return {
        "shard_count": len(regions),
        "regions": [r.to_regions_str() for r in regions],
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    # Requirement 17.7 / Design D10: the SDK region defaults to ap-southeast-1
    # when unset by the operator. Set both env vars boto3 consults.
    if not os.environ.get("AWS_DEFAULT_REGION"):
        os.environ["AWS_DEFAULT_REGION"] = args.region
    if not os.environ.get("AWS_REGION"):
        os.environ["AWS_REGION"] = args.region

    manifest: dict = json.loads(args.manifest.read_text(encoding="utf-8"))
    account_id = str(manifest.get("aws_account_id", "000000000000"))

    report: dict[str, object] = {"checks": {}}
    checks = report["checks"]
    assert isinstance(checks, dict)

    # --- 1) Region residency -------------------------------------------------
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - boto3 is a dev dep
        print(f"submit-run.py: boto3 is required ({exc})", file=sys.stderr)
        return 2
    s3 = boto3.client("s3", region_name=args.region)
    try:
        check_region_residency(manifest, s3)
        checks["region_residency"] = "OK"
    except RegionResidencyError as exc:
        checks["region_residency"] = f"FAIL: {exc}"
        if not args.dry_run:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3

    # --- 2) ECR residency ----------------------------------------------------
    try:
        image_uris = _image_uris_from_manifest(args.containers_manifest, account_id)
        check_ecr_residency(image_uris)
        checks["ecr_residency"] = f"OK ({len(image_uris)} images)"
    except (EcrResidencyError, FileNotFoundError) as exc:
        checks["ecr_residency"] = f"FAIL: {exc}"
        if not args.dry_run:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 4

    # --- 3) Resource overrides ----------------------------------------------
    try:
        resources = _resource_report(manifest)
        report["resources"] = resources
        checks["resources"] = "OK"
    except (KeyError, ValueError) as exc:
        checks["resources"] = f"FAIL: {exc}"
        if not args.dry_run:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 5

    # --- 4) Shard plan ------------------------------------------------------
    try:
        shard_by_chromosome = bool(manifest.get("shard_by_chromosome", True))
        shard_report = _shard_report(args.fai, shard_by_chromosome)
        if shard_report is not None:
            report["shard_plan"] = shard_report
            checks["shard_plan"] = f"OK ({shard_report['shard_count']} shards)"
        else:
            checks["shard_plan"] = "SKIPPED (no --fai provided)"
    except (ValueError, FileNotFoundError) as exc:
        checks["shard_plan"] = f"FAIL: {exc}"
        if not args.dry_run:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 6

    # --- 5) Instance selection ---------------------------------------------
    try:
        price_list = load_price_list(args.price_list)
        instances = _instance_report(resources, price_list)
        report["instances"] = instances
        checks["instance_selection"] = "OK"
    except (ValueError, FileNotFoundError) as exc:
        checks["instance_selection"] = f"FAIL: {exc}"
        if not args.dry_run:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 7

    if args.dry_run:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    # --- 6) StartRun --------------------------------------------------------
    omics = boto3.client("omics", region_name=args.region)
    storage_type = manifest.get("run_storage_type", "DYNAMIC")
    # HealthOmics rejects any key in `parameters` that isn't declared in
    # the workflow's parameter template. Strip submit-run-only convenience
    # fields (aws_account_id, run_cache_id, run_cache_behavior) before
    # passing the manifest through.
    _SUBMIT_ONLY_FIELDS = {"aws_account_id", "run_cache_id", "run_cache_behavior", "storage_capacity", "_comment_optimisations"}
    workflow_parameters = {
        k: v for k, v in manifest.items() if k not in _SUBMIT_ONLY_FIELDS
    }
    start_kwargs = {
        "workflowId": args.workflow_id,
        "name": f"aou-sv-{manifest['sample_id']}",
        "roleArn": args.role_arn,
        "parameters": workflow_parameters,
        "storageType": storage_type,
        # HealthOmics requires outputUri on every StartRun call. We take
        # it from the Input_Manifest's `output_prefix` so outputs land
        # exactly where the manifest declared they would.
        "outputUri": manifest["output_prefix"],
    }
    # STATIC storage requires an explicit capacity (in GB). Pass through
    # from the manifest's optional `storage_capacity` field.
    if storage_type == "STATIC" and manifest.get("storage_capacity"):
        start_kwargs["storageCapacity"] = int(manifest["storage_capacity"])
    # Requirement 17.12 / Design D16: pass cacheId through only when the
    # manifest both opts in and supplies one. HealthOmics raises
    # ``ValidationException`` for an empty cacheId, so we never pass it blank.
    if manifest.get("enable_run_cache") and manifest.get("run_cache_id"):
        start_kwargs["cacheId"] = manifest["run_cache_id"]
        start_kwargs["cacheBehavior"] = manifest.get("run_cache_behavior", "CACHE_ALWAYS")

    resp = omics.start_run(**start_kwargs)
    print(
        json.dumps(
            {"run_id": resp.get("id"), "status": resp.get("status"), "checks": checks},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
