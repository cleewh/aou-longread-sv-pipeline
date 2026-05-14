version 1.1

# wdl/tasks/pbmm2.wdl
#
# Task 6.2 — Pbmm2_Align task.
#
# Aligns an unaligned HiFi BAM to the supplied reference using pbmm2's
# CCS preset and emits a coordinate-sorted, BAI-indexed BAM named
# `<sample_id>.aligned.bam`.
#
# Requirements: 2.2, 11.1, 17.5
# Design: pbmm2_Align_Task contract, Resource defaults table
#         (cpu=16, memory=64 GB, disk=500 GB, arm64 primary).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 6.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real per-platform digest (arm64 primary per
# Requirement 17.5) on first push.

task Pbmm2_Align {
    input {
        File   hifi_reads_bam
        File   reference_fasta
        File   reference_fai
        String sample_id
        Int    cpu       = 16
        Int    memory_gb = 64
        Int    disk_gb   = 500
    }

    command <<<
        set -euo pipefail

        # Colocate the .fai next to the .fasta so pbmm2 / htslib can
        # discover the index without relative-path guesswork. HealthOmics
        # localises inputs to disjoint directories by default.
        FASTA_BASENAME="$(basename ~{reference_fasta})"
        ln -sf ~{reference_fasta} "$FASTA_BASENAME"
        ln -sf ~{reference_fai}   "${FASTA_BASENAME}.fai"

        pbmm2 align \
            "$FASTA_BASENAME" \
            ~{hifi_reads_bam} \
            ~{sample_id}.aligned.bam \
            --preset CCS \
            --sort \
            --bam-index BAI \
            --num-threads ~{cpu}
    >>>

    output {
        File aligned_bam = "~{sample_id}.aligned.bam"
        File aligned_bai = "~{sample_id}.aligned.bam.bai"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/pbmm2@sha256:3d9990b0b9d99911063348ab5ce288b202183c0ff73c628505ea9650e4909e7f"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
