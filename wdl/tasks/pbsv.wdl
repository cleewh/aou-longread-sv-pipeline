version 1.1

# wdl/tasks/pbsv.wdl
#
# Task 7.4 — PBSV_Discover_Task / PBSV_Merge_Svsig_Task / PBSV_Call_Task.
#
# PBSV (https://github.com/PacificBiosciences/pbsv) is PacBio's
# reference SV caller. It runs in two phases:
#
#   1. `pbsv discover <aligned_bam> <out.svsig.gz>`  — emits a per-sample
#      signature file. Optionally restricted to a single region via
#      `--region` so we can scatter per-chromosome when
#      `shard_by_chromosome=true`.
#   2. `pbsv call <reference> <svsig.gz>... <out.vcf>` — joint-calls
#      SVs from the union of all signatures. Accepts multiple svsig
#      files on the command line, which is what makes the
#      shard→merge→call topology practical.
#
# Per Design D7 + §Task graph, the WDL layer expresses this as three
# tasks so the scatter and the merge are visible to the HealthOmics
# scheduler (and observable in CloudWatch). The merge step is a thin
# `cat`: bgzip files concatenate losslessly, and `pbsv call` can
# consume the result as a single svsig input.
#
# All three tasks pin the amd64 sentinel digest: pbsv is amd64-only
# upstream (PacBio publishes only amd64 binaries; see Design Graviton
# matrix and containers/manifest.yaml `platforms: [linux/amd64]` for
# pbsv). This satisfies Requirement 17.6 ("fall back to amd64 when
# arm64 is unavailable upstream").
#
# Requirements: 5.1, 5.2, 5.3, 5.4, 11.1, 11.3, 17.6, 17.8
# Design: PBSV branch in task graph, D7, Resource defaults table
#         (PBSV_Discover: cpu=4, memory=16 GB, disk=200 GB;
#          PBSV_Merge_Svsig: cpu=2, memory=8 GB, disk=100 GB;
#          PBSV_Call: cpu=8, memory=64 GB, disk=200 GB; all amd64).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 7.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real amd64 digest on first push.

task PBSV_Discover_Task {
    input {
        File   aligned_bam
        File   aligned_bai
        String sample_id
        # Empty region ⇒ whole-sample discover. Non-empty ⇒ one
        # chromosome per shard (e.g. "chr1"). The workflow builds this
        # value from shard_planner.plan_shards() when
        # shard_by_chromosome=true.
        String region    = ""
        Int    cpu       = 4
        Int    memory_gb = 16
        Int    disk_gb   = 200
    }

    # Shard label baked into the svsig filename. An explicit
    # "unsharded" label distinguishes whole-sample from per-chromosome
    # mode and keeps filenames unique across any mix-mode accident.
    String shard_label = if region == "" then "unsharded" else region

    command <<<
        set -euo pipefail

        # Colocate BAM + BAI so htslib/pbsv discover the index without
        # relative-path guesswork. Same pattern as sniffles2.wdl.
        BAM_BASENAME="$(basename ~{aligned_bam})"
        ln -sf ~{aligned_bam} "${BAM_BASENAME}"
        ln -sf ~{aligned_bai} "${BAM_BASENAME}.bai"

        # `pbsv discover` takes the region as a CLI flag rather than a
        # BED file (in contrast to sniffles). Omit the flag entirely
        # when region is empty so pbsv runs over the whole BAM.
        REGION_FLAG=""
        if [[ -n "~{region}" ]]; then
            REGION_FLAG="--region ~{region}"
        fi

        pbsv discover \
            ${REGION_FLAG} \
            "${BAM_BASENAME}" \
            ~{sample_id}.~{shard_label}.svsig.gz
    >>>

    output {
        File svsig_gz = "~{sample_id}.~{shard_label}.svsig.gz"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/pbsv@sha256:bc6e5a867d30042138f3ef94471fbe16e8ad03efc1794466c56eb3c198db263f"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}

task PBSV_Merge_Svsig_Task {
    input {
        Array[File] svsig_shards
        String      sample_id
        Int         cpu       = 2
        Int         memory_gb = 8
        Int         disk_gb   = 100
    }

    command <<<
        set -euo pipefail

        # svsig.gz files are bgzipped. Bgzip streams concatenate
        # losslessly: `cat a.bgz b.bgz > c.bgz` produces a valid bgzip
        # stream that any bgzip-aware reader (including pbsv call) can
        # consume record-for-record. `pbsv call` also accepts multiple
        # svsig files on the command line directly, but keeping the
        # merge as its own task gives the scheduler a visible handle
        # and simplifies the downstream `pbsv call` invocation to a
        # single-file argument.
        cat ~{sep(" ", svsig_shards)} > ~{sample_id}.merged.svsig.gz
    >>>

    output {
        File merged_svsig = "~{sample_id}.merged.svsig.gz"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/pbsv@sha256:bc6e5a867d30042138f3ef94471fbe16e8ad03efc1794466c56eb3c198db263f"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}

task PBSV_Call_Task {
    input {
        Array[File] svsig_shards
        File        reference_fasta
        File        reference_fai
        String      sample_id
        Int         cpu       = 8
        Int         memory_gb = 64
        Int         disk_gb   = 200
    }

    command <<<
        set -euxo pipefail

        echo "[PBSV_Call] starting at $(date -Is)"
        echo "[PBSV_Call] n_svsig_shards=~{length(svsig_shards)}"
        echo "[PBSV_Call] reference_fasta=~{reference_fasta}"
        ls -la ~{sep(" ", svsig_shards)} ~{reference_fasta} ~{reference_fai} || true

        # Colocate FASTA + .fai so pbsv / htslib find the index.
        FASTA_BASENAME="$(basename ~{reference_fasta})"
        ln -sf ~{reference_fasta} "${FASTA_BASENAME}"
        ln -sf ~{reference_fai}   "${FASTA_BASENAME}.fai"

        echo "[PBSV_Call] invoking pbsv call at $(date -Is)"

        # `pbsv call --ccs` matches the Phase 1 upstream configuration
        # for PacBio HiFi CCS samples (Requirement 5.2). pbsv accepts
        # multiple svsig files as positional args and merges them
        # internally; passing per-chromosome shards directly avoids
        # the "Different number of chromosomes between svsig and
        # reference" error that `cat`-concatenation produced (the
        # duplicated per-shard @SQ headers multiplied the SVSIG
        # contig count by the shard count). pbsv writes uncompressed
        # VCF by default; we bgzip + tabix afterwards to match the
        # Output_Bundle schema.
        pbsv call \
            --ccs \
            -j ~{cpu} \
            "${FASTA_BASENAME}" \
            ~{sep(" ", svsig_shards)} \
            ~{sample_id}.sv.pbsv.vcf

        # --- Canonicalise the `##source=` header line. -----------------
        # Per Requirement 5.3 the final VCF carries `##source=pbsv`.
        # pbsv emits `##source=pbsv <version>` by default; rewrite it
        # to the canonical string without the version suffix. Tool
        # version is tracked separately in run_metadata.json
        # (ToolInfo.version).
        sed -i -E 's/^##source=pbsv.*$/##source=pbsv/' ~{sample_id}.sv.pbsv.vcf
        # Guarantee a `##source=pbsv` line even if pbsv emitted none.
        if ! grep -q '^##source=pbsv$' ~{sample_id}.sv.pbsv.vcf; then
            awk '/^#CHROM/ && !ins { print "##source=pbsv"; ins=1 } { print }' \
                ~{sample_id}.sv.pbsv.vcf > ~{sample_id}.sv.pbsv.with_source.vcf
            mv ~{sample_id}.sv.pbsv.with_source.vcf ~{sample_id}.sv.pbsv.vcf
        fi

        bgzip -@ ~{cpu} ~{sample_id}.sv.pbsv.vcf
        tabix -p vcf ~{sample_id}.sv.pbsv.vcf.gz
    >>>

    output {
        File sv_vcf     = "~{sample_id}.sv.pbsv.vcf.gz"
        File sv_vcf_tbi = "~{sample_id}.sv.pbsv.vcf.gz.tbi"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/pbsv@sha256:bc6e5a867d30042138f3ef94471fbe16e8ad03efc1794466c56eb3c198db263f"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
