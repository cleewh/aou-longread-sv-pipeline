#!/usr/bin/env python3
"""End-to-end HealthOmics orchestration (Task 16.6).

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 17.11
Design: D15, Test harness (``test/e2e/run_e2e.py``) runtime sequence.

Runtime sequence (mirrors Design §Test harness):

1.  Read ``test/e2e/inputs.json`` (staged S3 URIs + expected checksums).
2.  Invoke ``scripts/submit-run.py`` with the test manifest; capture ``run_id``.
3.  Poll ``aws omics get-run`` every 60 s until the run reaches a terminal
    state, cancelling via ``aws omics cancel-run`` at the
    ``wall_clock_hours_max`` budget (Requirement 14.9).
4.  Assert output objects exist at predicted S3 paths (14.3 / 14.5),
    including the ``.tbi`` sibling for every VCF we produced.
5.  ``bcftools view -h`` the harmonised VCF; assert the ``CALLERS`` INFO
    header is declared (14.3).
6.  ``bcftools view -r chr20 | grep -c`` SV record count ≥ 100 (14.4).
7.  Parse ``<sample>.run_metadata.json``; assert per-caller status
    ``succeeded`` for PAV, Sniffles2, PBSV (14.6).
8.  Run ``truvari bench`` against GIAB v0.6 chr20; assert recall ≥ 0.80
    and precision ≥ 0.80 (14.7).
9.  Parse Cost_Report from run_metadata.json, call
    :func:`cost_regression.evaluate`; emit non-fatal warning + rewrite
    baseline iff triggered (17.11).

The script exits non-zero on the first failing assertion, printing the
expected vs observed values.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Import ``cost_regression`` as a bare module; test/e2e/ is a package so we
# can also fall back to an explicit relative import when invoked as
# ``python -m test.e2e.run_e2e``.
_E2E_DIR = Path(__file__).resolve().parent
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))
import cost_regression  # noqa: E402


DEFAULT_REGION = "ap-southeast-1"
_REPO_ROOT = _E2E_DIR.parent.parent
_DEFAULT_INPUTS_JSON = _E2E_DIR / "inputs.json"
_DEFAULT_COST_BASELINE = _E2E_DIR / "cost_baseline.json"
_DEFAULT_TRUVARI_THRESHOLDS = _E2E_DIR / "truvari_thresholds.json"
_POLL_INTERVAL_SECONDS = 60
_TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "STOPPED", "DELETED"}


# ---------------------------------------------------------------------------
# Explicit exception classes per failure mode (Design §Test harness)
# ---------------------------------------------------------------------------


class E2EError(Exception):
    """Base class for all end-to-end assertion failures."""


class SubmissionError(E2EError):
    """submit-run.py failed to submit the run."""


class RunFailedError(E2EError):
    """HealthOmics reported a non-COMPLETED terminal status."""


class WallClockExceeded(E2EError):
    """Run exceeded the wall-clock budget and was cancelled (Req 14.9)."""


class OutputAssertionError(E2EError):
    """An expected output object / header / count assertion failed (Reqs 14.3-14.6)."""


class TruvariAssertionError(E2EError):
    """Truvari recall or precision fell below threshold (Req 14.7)."""


# ---------------------------------------------------------------------------
# CLI + arg parsing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_e2e.py",
        description=(
            "End-to-end smoke test for the AoU long-read SV HealthOmics "
            "pipeline. Submits, polls, and asserts per Design §Test harness."
        ),
    )
    p.add_argument(
        "--bucket",
        required=True,
        help="Target ap-southeast-1 S3 bucket (must match staged inputs).",
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
        help=f"AWS region (default {DEFAULT_REGION}).",
    )
    p.add_argument(
        "--inputs-json",
        type=Path,
        default=_DEFAULT_INPUTS_JSON,
        help="Path to test/e2e/inputs.json.",
    )
    p.add_argument(
        "--cost-baseline",
        type=Path,
        default=_DEFAULT_COST_BASELINE,
        help="Path to test/e2e/cost_baseline.json.",
    )
    p.add_argument(
        "--truvari-thresholds",
        type=Path,
        default=_DEFAULT_TRUVARI_THRESHOLDS,
        help="Path to test/e2e/truvari_thresholds.json.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan; do not submit or poll a real HealthOmics run.",
    )
    return p


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _S3Url:
    bucket: str
    key: str

    @classmethod
    def parse(cls, uri: str) -> "_S3Url":
        parsed = urlparse(uri)
        if parsed.scheme != "s3":
            raise ValueError(f"not an s3:// URI: {uri!r}")
        return cls(bucket=parsed.netloc, key=parsed.path.lstrip("/"))

    def to_uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def _run_cmd(
    cmd: list[str], *, capture: bool = True, check: bool = True, input_text: str | None = None
) -> subprocess.CompletedProcess:
    """Thin wrapper around ``subprocess.run`` with structured error output."""
    return subprocess.run(
        cmd,
        capture_output=capture,
        check=check,
        text=True,
        input=input_text,
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Steps 1-2: load inputs + submit
# ---------------------------------------------------------------------------


def submit_run(
    inputs: dict, *, workflow_id: str, role_arn: str, region: str, manifest_path: Path
) -> str:
    """Invoke scripts/submit-run.py with the submit_manifest from inputs.json.

    Returns the HealthOmics run_id parsed from submit-run.py's JSON stdout.
    """
    manifest_path.write_text(
        json.dumps(inputs["submit_manifest"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        str(_REPO_ROOT / "scripts" / "submit-run.py"),
        "--manifest",
        str(manifest_path),
        "--workflow-id",
        workflow_id,
        "--role-arn",
        role_arn,
        "--region",
        region,
    ]
    proc = _run_cmd(cmd, check=False)
    if proc.returncode != 0:
        raise SubmissionError(
            f"submit-run.py exited {proc.returncode}: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SubmissionError(
            f"submit-run.py did not emit JSON: {proc.stdout!r}"
        ) from exc
    run_id = parsed.get("run_id")
    if not run_id:
        raise SubmissionError(f"submit-run.py output missing run_id: {parsed!r}")
    return str(run_id)


# ---------------------------------------------------------------------------
# Step 3: poll with wall-clock cancellation (Req 14.9)
# ---------------------------------------------------------------------------


def poll_until_terminal(
    run_id: str,
    *,
    region: str,
    wall_clock_hours_max: float,
    poll_interval_seconds: int = _POLL_INTERVAL_SECONDS,
    now_fn=time.monotonic,
    sleep_fn=time.sleep,
) -> dict:
    """Poll ``aws omics get-run`` until a terminal status, cancelling at the budget.

    Returns the final get-run response dict. Raises :class:`WallClockExceeded`
    after cancelling the run when the wall-clock limit is exceeded.
    """
    deadline = now_fn() + wall_clock_hours_max * 3600.0
    while True:
        proc = _run_cmd(
            [
                "aws",
                "omics",
                "get-run",
                "--id",
                run_id,
                "--region",
                region,
                "--output",
                "json",
            ]
        )
        info = json.loads(proc.stdout)
        status = info.get("status", "UNKNOWN")
        if status in _TERMINAL_STATES:
            return info
        if now_fn() >= deadline:
            # Budget exceeded — cancel and raise.
            _run_cmd(
                [
                    "aws",
                    "omics",
                    "cancel-run",
                    "--id",
                    run_id,
                    "--region",
                    region,
                ],
                check=False,
            )
            raise WallClockExceeded(
                f"HealthOmics run {run_id} exceeded {wall_clock_hours_max} h budget "
                f"(last status: {status})"
            )
        sleep_fn(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Step 4: assert outputs exist at predicted S3 paths (Reqs 14.3, 14.5)
# ---------------------------------------------------------------------------


def assert_outputs_exist(
    s3_client,
    *,
    sample_id: str,
    output_prefix: str,
    expected_callers: list[str],
) -> dict[str, str]:
    """Head-check each predicted output basename under ``output_prefix``.

    Returns a ``{basename: s3_uri}`` map for downstream use. Raises
    :class:`OutputAssertionError` on any missing object or missing .tbi
    sibling.
    """
    # Import here so tests that exercise pure-logic helpers don't need
    # the metadata-writer package on sys.path.
    sys.path.insert(
        0, str(_REPO_ROOT / "containers" / "metadata-writer" / "src")
    )
    from output_namer import expected_output_basenames  # noqa: E402

    per_caller_status = {caller: "succeeded" for caller in expected_callers}
    basenames = expected_output_basenames(sample_id, per_caller_status)
    prefix_url = _S3Url.parse(output_prefix)
    found: dict[str, str] = {}
    missing: list[str] = []

    for basename in basenames:
        key = prefix_url.key + basename
        try:
            s3_client.head_object(Bucket=prefix_url.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - botocore.ClientError variants
            missing.append(f"{basename} ({exc})")
            continue
        uri = f"s3://{prefix_url.bucket}/{key}"
        found[basename] = uri
        # Req 14.5: every VCF must have a .tbi sibling.
        if basename.endswith(".vcf.gz"):
            tbi_key = key + ".tbi"
            try:
                s3_client.head_object(Bucket=prefix_url.bucket, Key=tbi_key)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{basename}.tbi sibling ({exc})")

    if missing:
        raise OutputAssertionError(
            f"Missing output objects under {output_prefix!r}: {missing!r}"
        )
    return found


# ---------------------------------------------------------------------------
# Steps 5-6: bcftools header + chr20 record count (Reqs 14.3, 14.4)
# ---------------------------------------------------------------------------


def assert_harmonised_vcf_shape(
    harmonised_vcf_s3: str,
    *,
    s3_client,
    workdir: Path,
    min_chr20_records: int,
) -> None:
    """Download the harmonised VCF locally and run bcftools header + count checks."""
    s3_url = _S3Url.parse(harmonised_vcf_s3)
    local_vcf = workdir / Path(s3_url.key).name
    s3_client.download_file(s3_url.bucket, s3_url.key, str(local_vcf))
    # Also pull the .tbi so bcftools can do region queries.
    s3_client.download_file(s3_url.bucket, s3_url.key + ".tbi", str(local_vcf) + ".tbi")

    header_proc = _run_cmd(["bcftools", "view", "-h", str(local_vcf)])
    if "##INFO=<ID=CALLERS" not in header_proc.stdout:
        raise OutputAssertionError(
            "Harmonised VCF header missing CALLERS INFO declaration. "
            f"Header preview: {header_proc.stdout[:400]!r}"
        )

    region_proc = _run_cmd(
        ["bcftools", "view", "-r", "chr20", str(local_vcf), "-Ov"]
    )
    count = sum(
        1 for line in region_proc.stdout.splitlines() if line and not line.startswith("#")
    )
    if count < min_chr20_records:
        raise OutputAssertionError(
            f"Harmonised chr20 SV record count {count} < minimum {min_chr20_records}"
        )


# ---------------------------------------------------------------------------
# Step 7: per-caller status from run_metadata.json (Req 14.6)
# ---------------------------------------------------------------------------


def assert_per_caller_succeeded(
    run_metadata: dict, *, expected_callers: list[str]
) -> None:
    per_caller = run_metadata.get("per_caller_status") or {}
    for caller in expected_callers:
        status = per_caller.get(caller)
        if status != "succeeded":
            raise OutputAssertionError(
                f"Caller {caller!r} did not succeed: status={status!r}; "
                f"per_caller_status={per_caller!r}"
            )


# ---------------------------------------------------------------------------
# Step 8: truvari bench (Req 14.7)
# ---------------------------------------------------------------------------


def assert_truvari_thresholds(
    *,
    base_vcf: Path,
    comp_vcf: Path,
    include_bed: Path,
    workdir: Path,
    min_recall: float,
    min_precision: float,
) -> dict[str, float]:
    """Run ``truvari bench`` and assert recall + precision ≥ thresholds."""
    out_dir = workdir / "truvari_out"
    # truvari insists the output directory does not already exist.
    if out_dir.exists():
        # Best-effort clean.
        for child in sorted(out_dir.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        out_dir.rmdir()
    _run_cmd(
        [
            "truvari",
            "bench",
            "--base",
            str(base_vcf),
            "--comp",
            str(comp_vcf),
            "--includebed",
            str(include_bed),
            "-o",
            str(out_dir),
        ]
    )
    summary = _load_json(out_dir / "summary.json")
    recall = float(summary.get("recall", 0.0))
    precision = float(summary.get("precision", 0.0))
    if recall < min_recall or precision < min_precision:
        raise TruvariAssertionError(
            f"Truvari thresholds not met: recall={recall:.3f} (min {min_recall}), "
            f"precision={precision:.3f} (min {min_precision})"
        )
    return {"recall": recall, "precision": precision}


# ---------------------------------------------------------------------------
# Step 9: cost regression (Req 17.11)
# ---------------------------------------------------------------------------


def evaluate_cost_regression(
    run_metadata: dict, *, baseline_path: Path
) -> tuple[bool, dict[str, Any]]:
    cost_report = run_metadata.get("cost_report") or {}
    observed_total = float(cost_report.get("total_estimated_usd", 0.0))
    warning_fired, new_baseline = cost_regression.evaluate(
        observed_total, baseline_path
    )
    if warning_fired:
        print(
            f"WARNING: cost regression detected — observed ${observed_total:.2f} "
            f"> baseline * 1.20. Baseline file rewritten: "
            f"new observed_total_usd={new_baseline['observed_total_usd']:.2f}",
            file=sys.stderr,
        )
    return warning_fired, new_baseline


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _download(s3_client, s3_uri: str, dest: Path) -> Path:
    url = _S3Url.parse(s3_uri)
    s3_client.download_file(url.bucket, url.key, str(dest))
    return dest


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915, PLR0912 - linear script
    args = _build_arg_parser().parse_args(argv)

    inputs = _load_json(args.inputs_json)
    truvari_thresholds = _load_json(args.truvari_thresholds)
    assertions = inputs["assertions"]
    submit_manifest = inputs["submit_manifest"]
    expected_callers = list(assertions.get("expected_callers", ["PAV", "Sniffles2", "pbsv"]))
    sample_id = submit_manifest["sample_id"]
    output_prefix = submit_manifest["output_prefix"]

    # Region defaulting: HealthOmics + residency gate both assume ap-southeast-1.
    os.environ.setdefault("AWS_DEFAULT_REGION", args.region)
    os.environ.setdefault("AWS_REGION", args.region)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "plan": "e2e",
                    "bucket": args.bucket,
                    "workflow_id": args.workflow_id,
                    "role_arn": args.role_arn,
                    "region": args.region,
                    "sample_id": sample_id,
                    "output_prefix": output_prefix,
                    "expected_callers": expected_callers,
                    "assertions": assertions,
                    "truvari_thresholds": truvari_thresholds,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - boto3 is a dev dep
        print(f"run_e2e.py: boto3 is required ({exc})", file=sys.stderr)
        return 2

    s3 = boto3.client("s3", region_name=args.region)

    with tempfile.TemporaryDirectory(prefix="aou-e2e-") as tmp:
        workdir = Path(tmp)
        manifest_path = workdir / "submit_manifest.json"

        # Step 1-2: submit.
        run_id = submit_run(
            inputs,
            workflow_id=args.workflow_id,
            role_arn=args.role_arn,
            region=args.region,
            manifest_path=manifest_path,
        )
        print(f"[run_e2e] submitted run {run_id}", flush=True)

        # Step 3: poll until terminal / cancel at budget.
        wall_clock_hours_max = float(assertions.get("wall_clock_hours_max", 6))
        try:
            info = poll_until_terminal(
                run_id,
                region=args.region,
                wall_clock_hours_max=wall_clock_hours_max,
            )
        except WallClockExceeded as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3

        if info.get("status") != "COMPLETED":
            raise RunFailedError(
                f"run {run_id} terminated with status {info.get('status')!r}: "
                f"{info.get('statusMessage', '')}"
            )
        print(f"[run_e2e] run {run_id} COMPLETED", flush=True)

        # Step 4: outputs exist.
        found = assert_outputs_exist(
            s3,
            sample_id=sample_id,
            output_prefix=output_prefix,
            expected_callers=expected_callers,
        )
        print(f"[run_e2e] found {len(found)} output objects", flush=True)

        # Step 5-6: harmonised VCF shape.
        harmonised_basename = f"{sample_id}.sv.harmonised.vcf.gz"
        if harmonised_basename not in found:
            raise OutputAssertionError(
                f"Harmonised VCF {harmonised_basename!r} not in output set: {list(found)!r}"
            )
        assert_harmonised_vcf_shape(
            found[harmonised_basename],
            s3_client=s3,
            workdir=workdir,
            min_chr20_records=int(assertions.get("min_harmonised_sv_records_chr20", 100)),
        )
        print("[run_e2e] harmonised VCF header + chr20 count OK", flush=True)

        # Step 7: per-caller statuses from run_metadata.json.
        metadata_basename = f"{sample_id}.run_metadata.json"
        if metadata_basename not in found:
            raise OutputAssertionError(
                f"run_metadata.json {metadata_basename!r} not in output set"
            )
        local_metadata = _download(
            s3, found[metadata_basename], workdir / metadata_basename
        )
        run_metadata = _load_json(local_metadata)
        assert_per_caller_succeeded(run_metadata, expected_callers=expected_callers)
        print(
            f"[run_e2e] per-caller status OK for {expected_callers}",
            flush=True,
        )

        # Step 8: Truvari.
        giab_vcf_local = _download(
            s3,
            inputs["truth_set"]["giab_sv_vcf"]["s3_uri"],
            workdir / "HG002_SVs_Tier1_v0.6.vcf.gz",
        )
        giab_bed_local = _download(
            s3,
            inputs["truth_set"]["giab_sv_bed"]["s3_uri"],
            workdir / "HG002_SVs_Tier1_v0.6.bed",
        )
        # Download the harmonised comp VCF too (already downloaded by
        # assert_harmonised_vcf_shape; re-derive the path here).
        comp_vcf = workdir / harmonised_basename
        if not comp_vcf.exists():
            comp_vcf = _download(s3, found[harmonised_basename], comp_vcf)
        metrics = assert_truvari_thresholds(
            base_vcf=giab_vcf_local,
            comp_vcf=comp_vcf,
            include_bed=giab_bed_local,
            workdir=workdir,
            min_recall=float(truvari_thresholds["min_recall"]),
            min_precision=float(truvari_thresholds["min_precision"]),
        )
        print(
            f"[run_e2e] truvari OK recall={metrics['recall']:.3f} "
            f"precision={metrics['precision']:.3f}",
            flush=True,
        )

        # Step 9: cost regression.
        warning_fired, _ = evaluate_cost_regression(
            run_metadata, baseline_path=args.cost_baseline
        )
        print(
            f"[run_e2e] cost regression warning_fired={warning_fired}",
            flush=True,
        )

    print(f"[run_e2e] run {run_id} passed all assertions", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EError as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
