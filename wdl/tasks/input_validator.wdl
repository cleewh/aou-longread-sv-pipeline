version 1.1

# wdl/tasks/input_validator.wdl
#
# Task 6.1 — InputValidator task.
#
# Runs the metadata-writer container's `validate` subcommand against the
# serialised Input_Manifest JSON and emits a Boolean + String pair the
# workflow uses to decide whether to proceed with caller tasks.
#
# Requirements: 1.5, 2.1, 2.4, 2.5, 2.6, 6.5, 11.1
# Design: InputValidator_Task contract, Layer 2 error table, Resource
#         defaults table (cpu=1, memory=2 GB, disk=10 GB).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 6.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real per-platform digest on first push.

task InputValidator {
    input {
        File   manifest_json
        String pipeline_version = "unknown"
        String git_commit       = "unknown"
        Int    cpu         = 1
        Int    memory_gb   = 2
        Int    disk_gb     = 10
    }

    command <<<
        set -euo pipefail

        # Task 19.2 — workflow-level start-of-run log line (Req 12.3).
        # ------------------------------------------------------------
        # The first line of stdout from the first task in every run
        # carries a structured "workflow_start" JSON object with the
        # pipeline version, the git commit SHA of the repo at deploy
        # time, and the SHA-256 of the Input_Manifest. CloudWatch
        # surfaces this as the earliest audit-grade log line for every
        # HealthOmics run. The embedded Python block reads the two
        # version inputs from environment variables so the heredoc does
        # not need shell interpolation to survive the validator's later
        # parsing of stdout.
        export PIPELINE_VERSION='~{pipeline_version}'
        export GIT_COMMIT='~{git_commit}'
        export MANIFEST_JSON='~{manifest_json}'
        python - <<'PY'
import hashlib
import json
import os
import pathlib

manifest_path = pathlib.Path(os.environ["MANIFEST_JSON"])
manifest_bytes = manifest_path.read_bytes()
log = {
    "event": "workflow_start",
    "pipeline_version": os.environ.get("PIPELINE_VERSION", "unknown"),
    "git_commit": os.environ.get("GIT_COMMIT", "unknown"),
    "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
}
print(json.dumps(log, separators=(",", ":")))
PY

        # Emit a single-line JSON of the form {"valid": bool, "error": str}
        # to result.json. `validate` exits 0 on valid and 2 on invalid; we
        # do NOT propagate the non-zero exit from the validator — the
        # workflow reads the boolean from outputs and decides what to do.
        python -m metadata_writer validate \
            --manifest ~{manifest_json} \
            > result.json \
            || true

        # Parse with a small, dependency-free Python block so this task
        # does not require jq to be present in the metadata-writer image.
        # `validate` prints two JSON lines to stdout (per Design §Layer
        # 3 / Task 19.1): first the validator payload, then the task
        # trailer. We take the first non-empty JSON line as the
        # validator payload and treat any subsequent line as an
        # aggregateable trailer — this WDL task only needs the first.
        python - <<'PY'
import json, pathlib, sys

payload = None
lines = pathlib.Path("result.json").read_text(encoding="utf-8").splitlines()
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError:
        continue
    # The validator payload has `valid` and `error`; the trailer has
    # `task` + `status` + `exit_code`. Pick the first validator-shaped
    # line; fall back to the first parseable JSON line otherwise.
    if isinstance(candidate, dict) and "valid" in candidate:
        payload = candidate
        break
    if payload is None:
        payload = candidate

if not isinstance(payload, dict):
    payload = {"valid": False,
               "error": f"validator emitted non-JSON output: {payload!r}"}

valid = bool(payload.get("valid", False))
error = str(payload.get("error", ""))

pathlib.Path("is_valid.txt").write_text("true" if valid else "false", encoding="utf-8")
pathlib.Path("error_message.txt").write_text(error, encoding="utf-8")
PY
    >>>

    output {
        Boolean is_valid      = read_boolean("is_valid.txt")
        String  error_message = read_string("error_message.txt")
    }

    runtime {
        docker:  "000000000000.dkr.ecr.us-east-1.amazonaws.com/aou-sv/metadata-writer@sha256:8499b9b304fbaa4617ceb5c22a80a72d2b807c452bd3f5f57c5061a734e7c92a"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
