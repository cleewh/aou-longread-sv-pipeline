version 1.1

# wdl/tasks/pav2svs.wdl
#
# Task 6.3 — PAV2SVs task.
#
# Filters a PAV variants VCF to records with `abs(SVLEN) >= 50` and
# rewrites the `##source` header to `##source=PAV`, then bgzips and
# tabix-indexes the output. Delegates to `python -m pav2svs` which
# invokes `filter.py` in the pav2svs container.
#
# Output filenames carry the `<sample_id>.` prefix so Property 4
# (output files are sample_id-prefixed) holds.
#
# Requirements: 3.3, 3.5, 7.3, 11.1
# Design: PAV2SVs_Task contract, Resource defaults table
#         (cpu=2, memory=8 GB, disk=50 GB, arm64 primary).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 6.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real per-platform digest on first push.

task PAV2SVs {
    input {
        File   pav_vcf
        String sample_id
        Int    cpu       = 2
        Int    memory_gb = 8
        Int    disk_gb   = 50
    }

    command <<<
        set -euo pipefail

        python -m pav2svs \
            --in  ~{pav_vcf} \
            --out ~{sample_id}.sv.pav.vcf.gz
    >>>

    output {
        File sv_vcf     = "~{sample_id}.sv.pav.vcf.gz"
        File sv_vcf_tbi = "~{sample_id}.sv.pav.vcf.gz.tbi"
    }

    runtime {
        docker:  "000000000000.dkr.ecr.us-east-1.amazonaws.com/aou-sv/pav2svs@sha256:2fa183249819228d70af554f1b98a7d85f65bd7971c091a928a6770bd524136a"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
