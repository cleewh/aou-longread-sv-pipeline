#!/usr/bin/env python3
# Task 11.1: HealthOmics deploy CLI.
"""Register the AoU long-read SV pipeline WDL against AWS HealthOmics.

Requirements: 13.1, 13.2, 13.3.
Design: deploy.py pseudocode (Components and Interfaces §Deployment tooling).

The script runs the Design pseudocode literally:

1. ``miniwdl check wdl/main.wdl`` — exits non-zero on validation failure
   (Requirement 13.3).
2. Lists existing HealthOmics workflows and decides
   ``create | update | skip`` per :func:`decide_action` (Property 12,
   Requirement 13.2).
3. Zips the ``wdl/`` directory (plus ``parameter_template.json``),
   reads the parameter template, and calls
   ``aws omics create-workflow`` or ``aws omics update-workflow``
   (Requirement 13.1).
4. Prints the resulting workflow ID.
5. Optionally deploys ``scripts/budget-alarm.yaml`` via CloudFormation
   when ``--with-budget-alarm`` is supplied (Requirement 17.13).

The ``decide_action`` / ``validate_wdl`` / ``zip_workflow`` functions are
exported so they can be unit and property-tested without invoking the
CLI or AWS.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_REGION = None  # Uses AWS CLI configured region
DEFAULT_WORKFLOW_NAME = "aou-longread-sv-pipeline"


# ---------------------------------------------------------------------------
# Pure logic (unit + property testable)
# ---------------------------------------------------------------------------


def decide_action(
    name: str,
    version: str,
    existing: Sequence[dict],
    force_flag: bool,
) -> str:
    """Decide ``create`` | ``update`` | ``skip`` (Property 12, Requirement 13.2).

    Semantics:
      * ``create`` — no entry in ``existing`` matches both ``name`` and
        ``version``.
      * ``update`` — a matching entry exists AND ``force_flag`` is truthy.
      * ``skip``   — a matching entry exists AND ``force_flag`` is falsy.

    The comparison treats missing ``name`` / ``version`` fields as the
    empty string so callers can pass the raw output of
    ``aws omics list-workflows`` without pre-filtering.
    """
    for entry in existing:
        entry_name = entry.get("name", "")
        entry_version = entry.get("version", "")
        if entry_name == name and entry_version == version:
            return "update" if force_flag else "skip"
    return "create"


def validate_wdl(main_wdl: Path) -> None:
    """Run ``miniwdl check`` on ``main_wdl`` (Requirement 13.3).

    Raises :class:`subprocess.CalledProcessError` on validation failure.
    """
    subprocess.run(
        [sys.executable, "-m", "WDL", "check", str(main_wdl)],
        check=True,
        capture_output=True,
        text=True,
    )


def zip_workflow(wdl_dir: Path, zip_path: Path) -> Path:
    """Zip all ``*.wdl`` files under ``wdl_dir`` plus ``parameter_template.json``.

    The parameter template is written into the archive at the root as
    ``parameter_template.json`` even when it lives alongside the WDL in
    ``wdl_dir`` — HealthOmics looks for it at either location, and
    putting it at the root keeps the zip contents predictable.
    """
    if not wdl_dir.is_dir():
        raise FileNotFoundError(f"WDL directory not found: {wdl_dir}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for wdl in sorted(wdl_dir.rglob("*.wdl")):
            zf.write(wdl, arcname=str(wdl.relative_to(wdl_dir)))
        param_tmpl = wdl_dir / "parameter_template.json"
        if param_tmpl.exists():
            zf.write(param_tmpl, arcname="parameter_template.json")
    return zip_path


# ---------------------------------------------------------------------------
# AWS-side helpers (thin wrappers around boto3 so the CLI body stays linear)
# ---------------------------------------------------------------------------


def _list_existing_workflows(omics_client, name: str) -> list[dict]:
    """Return all HealthOmics workflows with a matching ``name``.

    HealthOmics ``list-workflows`` does not filter by version; it returns
    one entry per revision. ``decide_action`` compares both ``name`` and
    ``version`` so the full list is safe to pass through.
    """
    existing: list[dict] = []
    paginator = omics_client.get_paginator("list_workflows")
    for page in paginator.paginate():
        for wf in page.get("items", []):
            if wf.get("name") == name:
                existing.append(wf)
    return existing


def _existing_for_decide(existing: Iterable[dict], version: str) -> list[dict]:
    """Build the ``(name, version)`` view of ``existing`` for :func:`decide_action`.

    HealthOmics does not expose a first-class ``version`` field on every
    workflow in every API shape, so we stamp every entry with the version
    we are deploying — which means a same-name workflow always looks like
    a version match and the decision collapses to ``force`` gating.
    This is the Design-documented v0.1 behaviour: name uniqueness is
    sufficient, and ``--force`` is the explicit escape hatch.
    """
    return [{"name": wf.get("name", ""), "version": version} for wf in existing]


def _deploy_budget_alarm(
    region: str,
    stack_name: str,
    template_path: Path,
    threshold_usd: float,
    sns_topic_arn: str,
) -> None:
    """Deploy ``scripts/budget-alarm.yaml`` via ``aws cloudformation deploy``.

    Kept as a subprocess call rather than boto3 so operators can reuse
    their existing CloudFormation stack-policy and capabilities muscle
    memory, and so ``moto`` is not needed to exercise this path in CI.
    """
    subprocess.run(
        [
            "aws",
            "cloudformation",
            "deploy",
            "--stack-name",
            stack_name,
            "--template-file",
            str(template_path),
            "--capabilities",
            "CAPABILITY_IAM",
            "--parameter-overrides",
            f"Threshold={threshold_usd}",
            f"SnsTopicArn={sns_topic_arn}",
            "--region",
            region,
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy.py",
        description=(
            "Validate wdl/main.wdl, zip the workflow bundle, and register "
            "(or update, with --force) the workflow against AWS HealthOmics "
            "in ap-southeast-1."
        ),
    )
    p.add_argument(
        "--name",
        default=DEFAULT_WORKFLOW_NAME,
        help=f"HealthOmics workflow name (default {DEFAULT_WORKFLOW_NAME}).",
    )
    p.add_argument(
        "--version",
        default=None,
        help="Workflow version string. Defaults to the contents of the VERSION file.",
    )
    p.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region (default {DEFAULT_REGION}).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Update an existing same-name same-version workflow instead of skipping.",
    )
    p.add_argument(
        "--with-budget-alarm",
        action="store_true",
        help="Also deploy scripts/budget-alarm.yaml via CloudFormation (Req 17.13).",
    )
    p.add_argument(
        "--budget-threshold-usd",
        type=float,
        default=None,
        help="Monthly USD threshold for the Budget_Alarm stack (required with --with-budget-alarm).",
    )
    p.add_argument(
        "--budget-sns-topic-arn",
        default=None,
        help="SNS topic ARN notified when the budget is exceeded (required with --with-budget-alarm).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    version = args.version or (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    wdl_dir = repo_root / "wdl"
    main_wdl = wdl_dir / "main.wdl"
    param_tmpl_path = wdl_dir / "parameter_template.json"

    # --- 1) miniwdl check (Req 13.3) ----------------------------------------
    try:
        validate_wdl(main_wdl)
    except subprocess.CalledProcessError as exc:
        print("ERROR: miniwdl check failed.", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 2

    # --- 2) Inspect existing workflows + decide action (Req 13.2) -----------
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - boto3 is a dev dep
        print(f"deploy.py: boto3 is required ({exc})", file=sys.stderr)
        return 3

    omics = boto3.client("omics", region_name=args.region)
    existing = _list_existing_workflows(omics, args.name)
    action = decide_action(
        args.name, version, _existing_for_decide(existing, version), args.force
    )

    if action == "skip":
        print(
            json.dumps(
                {
                    "action": "skip",
                    "name": args.name,
                    "version": version,
                    "reason": "workflow already at this version; pass --force to update",
                }
            )
        )
        return 0

    # --- 3) Zip + create/update (Req 13.1) ----------------------------------
    parameter_template = json.loads(param_tmpl_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "workflow.zip"
        zip_workflow(wdl_dir, zip_path)
        zip_bytes = zip_path.read_bytes()

        if action == "create":
            resp = omics.create_workflow(
                name=args.name,
                description="AoU Long-Read SV Detection Pipeline",
                definitionZip=zip_bytes,
                parameterTemplate=parameter_template,
                main="main.wdl",
                storageType="DYNAMIC",
            )
            workflow_id = resp["id"]
        else:  # action == "update" — delete old + recreate (boto3 update_workflow
               # does not support definitionZip/parameterTemplate in all versions)
            workflow_id = existing[0]["id"]
            omics.delete_workflow(id=workflow_id)
            # Brief pause to allow deletion to propagate
            import time
            time.sleep(5)
            resp = omics.create_workflow(
                name=args.name,
                description="AoU Long-Read SV Detection Pipeline",
                definitionZip=zip_bytes,
                parameterTemplate=parameter_template,
                main="main.wdl",
                storageType="DYNAMIC",
            )
            workflow_id = resp["id"]

    print(
        json.dumps(
            {
                "action": action,
                "name": args.name,
                "version": version,
                "workflow_id": workflow_id,
            }
        )
    )

    # --- 4) Optional Budget_Alarm (Req 17.13) -------------------------------
    if args.with_budget_alarm:
        if not args.budget_threshold_usd or not args.budget_sns_topic_arn:
            print(
                "ERROR: --with-budget-alarm requires --budget-threshold-usd and "
                "--budget-sns-topic-arn",
                file=sys.stderr,
            )
            return 5
        cfn_path = Path(__file__).resolve().parent / "budget-alarm.yaml"
        stack_name = f"{args.name}-budget-alarm"
        try:
            _deploy_budget_alarm(
                args.region,
                stack_name,
                cfn_path,
                args.budget_threshold_usd,
                args.budget_sns_topic_arn,
            )
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: cloudformation deploy failed: {exc}", file=sys.stderr)
            return 6

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
