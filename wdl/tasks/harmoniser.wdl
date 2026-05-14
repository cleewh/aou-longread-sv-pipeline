version 1.1

# wdl/tasks/harmoniser.wdl
#
# Task 7.5 — Harmoniser_Task.
#
# Combines the per-caller SV_VCFs (PAV2SVs, Sniffles2, PBSV) into a
# single reconciled per-sample VCF with a `CALLERS` INFO tag on every
# record. Delegates to `python -m harmoniser` which is the container's
# entry point (see containers/harmoniser/src/__main__.py and
# run_harmoniser.py).
#
# Conditional input handling (Requirement 6.1 / 6.4 / 6.5):
#   * Runs on any non-empty subset of {PAV2SVs, Sniffles2, PBSV} VCFs.
#   * Fails with the Layer 3 error `"No SV callers produced output"`
#     (exit code 2 from run_harmoniser.py) when every input is absent
#     OR every provided VCF has zero SV records.
#
# Filter override (Requirement 6.6): when the operator supplies a JSON
# file via `harmoniser_filter_override_json`, it is passed verbatim to
# `--filter-override`. Otherwise the harmoniser uses the thresholds
# baked into `callset_integration_phase2`.
#
# Note on localisation: every File? input is nullable at the WDL layer;
# we let the engine skip file localisation when the input is absent and
# build the per-caller flag list inside the command block using
# HealthOmics-compatible shell guards rather than WDL string
# interpolation tricks. The usual WDL idiom
# `~{"--pav " + pav_sv_vcf}` interpolates an empty string when
# pav_sv_vcf is None, which conveniently drops the flag entirely — but
# we spell the tests out explicitly here so the command block reads as
# it would in any portable shell script.
#
# Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 11.1
# Design: Harmoniser_Task contract, D3, Resource defaults table
#         (cpu=8, memory=32 GB, disk=200 GB, arm64 primary).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 7.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real per-platform digest on first push (arm64
# primary per Requirement 17.5).

task Harmoniser {
    input {
        # All three per-caller VCFs are optional — the harmoniser runs
        # on any non-empty subset. When every input is absent the
        # container exits with code 2 and the Layer 3 error string
        # "No SV callers produced output".
        File?  pav_sv_vcf
        File?  sniffles2_sv_vcf
        File?  pbsv_sv_vcf

        # Optional JSON with filter threshold overrides. Matches the
        # `--filter-override` flag of run_harmoniser.py (Requirement 6.6).
        File?  harmoniser_filter_override_json

        String sample_id
        Int    cpu       = 8
        Int    memory_gb = 32
        Int    disk_gb   = 200
    }

    command <<<
        set -euo pipefail

        # Build the per-caller flag list. Each WDL-interpolated
        # expression below follows the pattern
        #    (placeholder) "--flag " plus optional path
        # so that when the optional is None the whole expression
        # evaluates to an empty string and the flag is dropped, and
        # when the optional is a File the flag + path are emitted.
        # This is the canonical WDL 1.1 idiom for optional CLI flags
        # and avoids a pre-command Python block.
        python -m harmoniser \
            ~{"--pav " + pav_sv_vcf} \
            ~{"--sniffles2 " + sniffles2_sv_vcf} \
            ~{"--pbsv " + pbsv_sv_vcf} \
            ~{"--filter-override " + harmoniser_filter_override_json} \
            --out ~{sample_id}.sv.harmonised.vcf.gz
    >>>

    output {
        File harmonised_sv_vcf     = "~{sample_id}.sv.harmonised.vcf.gz"
        File harmonised_sv_vcf_tbi = "~{sample_id}.sv.harmonised.vcf.gz.tbi"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/harmoniser@sha256:e60133ad11d00c604249f54db3a3cbeec415dff18d8a72cc44907c3b7cd10c75"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
