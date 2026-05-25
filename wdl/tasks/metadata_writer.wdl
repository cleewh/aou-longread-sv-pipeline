version 1.1

# wdl/tasks/metadata_writer.wdl
#
# Task 6.4 — MetadataWriter task.
#
# Always the last task in the workflow (Design D8). Assembles
# `<sample_id>.run_metadata.json` from per-caller status signals, tool
# versions, image digests, HealthOmics run identifiers, and a merged
# Cost_Report. Delegates to `python -m metadata_writer write` which
# invokes `writer.py` (Task 3.8) inside the metadata-writer container.
#
# Structs are imported from ../structs.wdl so that main.wdl can reuse
# the same types when wiring per-caller status through the workflow.
# WDL 1.1 requires shared struct definitions to be imported rather than
# redeclared; a redeclared struct is flagged as a duplicate-struct
# conflict by the miniwdl type checker even when the definitions are
# byte-identical.
#
# Requirements: 7.2, 7.3, 11.1, 12.2, 16.2, 17.10
# Design: D8, D9, MetadataWriter_Task contract, Resource defaults table
#         (cpu=1, memory=2 GB, disk=10 GB, arm64 primary).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 6.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real per-platform digest on first push.

import "../structs.wdl"

task MetadataWriter {
    input {
        String sample_id
        String pipeline_version
        String git_commit
        File   input_manifest

        String region           = "ap-southeast-1"
        String healthomics_run_id
        String workflow_id
        String workflow_name
        String workflow_version
        String start_time
        String end_time
        String status
        String storage_type     = "DYNAMIC"

        PerCallerStatus per_caller_status

        # Tool metadata — ordered arrays keyed by `tool_names`. writer.py
        # expects a JSON map `{name: {version, image_digest}}`; we
        # assemble that map at command-time from the three parallel
        # arrays to keep the WDL inputs flat and easy to wire from
        # main.wdl. Per Design §D9 the required tools are hifiasm, pav,
        # pav2svs, sniffles2, pbsv, pbmm2, harmoniser.
        Array[String] tool_names
        Array[String] tool_versions
        Array[String] tool_digests

        # Per-task cost records merged into Cost_Report by cost_report.py.
        Array[File]   cost_records_json

        # Output name -> S3 URI map, passed straight through to writer.py.
        Map[String, String] outputs

        # Pure ordering dependency on Harmoniser's output VCF so WDL
        # schedules MetadataWriter AFTER Harmoniser (and therefore
        # after every caller) completes. The file is localised but its
        # content is not read by writer.py — Harmoniser's URI is
        # already present in the `outputs` map above.
        File? harmonised_sv_vcf_dep

        Int cpu       = 1
        Int memory_gb = 2
        Int disk_gb   = 10
    }

    # Serialise struct / array / map inputs to JSON files so writer.py's
    # file-path arguments can consume them unchanged.
    File per_caller_status_json = write_json(per_caller_status)
    File outputs_json            = write_json(outputs)
    File tool_names_json         = write_json(tool_names)
    File tool_versions_json      = write_json(tool_versions)
    File tool_digests_json       = write_json(tool_digests)
    File cost_records_manifest   = write_lines(cost_records_json)

    command <<<
        set -euo pipefail

        # --- Assemble tool_info.json ---------------------------------------
        # writer.py expects {name: {version, image_digest}} (see
        # _REQUIRED_TOOLS in writer.py). The three parallel arrays are
        # zipped here so the WDL signature stays simple.
        python - <<'PY'
import json, pathlib

names    = json.loads(pathlib.Path("~{tool_names_json}").read_text(encoding="utf-8"))
versions = json.loads(pathlib.Path("~{tool_versions_json}").read_text(encoding="utf-8"))
digests  = json.loads(pathlib.Path("~{tool_digests_json}").read_text(encoding="utf-8"))

if not (len(names) == len(versions) == len(digests)):
    raise SystemExit(
        f"tool_names / tool_versions / tool_digests length mismatch: "
        f"{len(names)} / {len(versions)} / {len(digests)}"
    )

tool_info = {
    name: {"version": version, "image_digest": digest}
    for name, version, digest in zip(names, versions, digests)
}
pathlib.Path("tool_info.json").write_text(
    json.dumps(tool_info, sort_keys=True), encoding="utf-8"
)
PY

        # --- Merge per-task cost records into a single JSON array ---------
        python - <<'PY'
import json, pathlib

manifest = pathlib.Path("~{cost_records_manifest}").read_text(encoding="utf-8")
paths = [line for line in manifest.splitlines() if line.strip()]

records = []
for path in paths:
    entry = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if isinstance(entry, list):
        records.extend(entry)
    else:
        records.append(entry)

pathlib.Path("cost_records.json").write_text(
    json.dumps(records, sort_keys=True), encoding="utf-8"
)
PY

        # --- Invoke the writer --------------------------------------------
        python -m metadata_writer write \
            --sample-id          ~{sample_id} \
            --pipeline-version   ~{pipeline_version} \
            --git-commit         ~{git_commit} \
            --input-manifest     ~{input_manifest} \
            --region             ~{region} \
            --healthomics-run-id ~{healthomics_run_id} \
            --workflow-id        ~{workflow_id} \
            --workflow-name      ~{workflow_name} \
            --workflow-version   ~{workflow_version} \
            --start-time         ~{start_time} \
            --end-time           ~{end_time} \
            --status             ~{status} \
            --storage-type       ~{storage_type} \
            --per-caller-status  ~{per_caller_status_json} \
            --tool-info          tool_info.json \
            --outputs            ~{outputs_json} \
            --cost-records       cost_records.json
    >>>

    output {
        File run_metadata_json = "~{sample_id}.run_metadata.json"
    }

    runtime {
        docker:  "000000000000.dkr.ecr.us-east-1.amazonaws.com/aou-sv/metadata-writer@sha256:8499b9b304fbaa4617ceb5c22a80a72d2b807c452bd3f5f57c5061a734e7c92a"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
