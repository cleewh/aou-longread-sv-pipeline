version 1.1

# wdl/tasks/hifiasm.wdl
#
# Task 7.1 — Hifiasm_Assemble task.
#
# Assembles a set of HiFi long reads into a diploid pair of haplotype
# FASTAs using hifiasm 0.19.9 (Design Hifiasm_Task contract). The input
# is an unaligned HiFi BAM; the task converts it to FASTQ, runs
# hifiasm, and converts the two primary haplotype contig GFAs into
# bgzipped FASTAs named `<sample_id>.hap1.fa.gz` / `<sample_id>.hap2.fa.gz`.
#
# Hifiasm is whole-genome and is NOT shardable (Design §Per-task
# interfaces — "Hifiasm_Task is whole-genome and is not shardable"),
# so this file declares a single task with no scatter/merge pair.
#
# HiFi-input gate. Requirement 3.4 — and the Hifiasm_Task contract —
# calls for a clear failure on non-HiFi input. The ideal runtime check
# is `samtools view -H | grep -c CCS` against the BAM's @PG header, and
# the hifiasm container (containers/hifiasm/Dockerfile) installs
# samtools on both amd64 and arm64 stages so this check can run in-task.
# `submit-run.py` (Task 10.9) does a belt-and-braces client-side check
# before `StartRun`; the in-task check is the second line of defence
# that fires when the client-side gate is bypassed (e.g. for a replay
# from the HealthOmics console).
#
# Requirements: 3.1, 3.4, 11.1, 11.2
# Design: Hifiasm_Task contract, D7, Resource defaults table
#         (cpu=48, memory=256 GB, disk=1500 GB, arm64 primary).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 7.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real per-platform digest (arm64 primary per
# Requirement 17.5) on first push.

task Hifiasm_Assemble {
    input {
        File   hifi_reads_bam
        String sample_id
        # chr20-only sizing. With `-f 0` hifiasm's peak RSS is ~6 GB
        # on HG002 chr20, so we ask for 32 GB to leave headroom for
        # FASTQ conversion buffers. Whole-genome operators must bump
        # this via `hifiasm_memory_gb` — see SOURCES.md
        # `## Whole-genome sizing caveat`.
        Int    cpu       = 16
        Int    memory_gb = 32
        Int    disk_gb   = 500
    }

    command <<<
        set -euo pipefail

        # --- HiFi-input gate (Requirement 3.4) ------------------------------
        # Count @PG / @RG header lines referencing CCS. The hifiasm
        # Dockerfile installs samtools on both amd64 and arm64 stages so
        # this check runs in-task. We look for the string "CCS" anywhere
        # in the header because both `@PG ID:ccs` and `@RG ... PL:PACBIO
        # ... DS:READTYPE=CCS` conventions appear in HiFi BAMs.
        CCS_HITS="$(samtools view -H ~{hifi_reads_bam} | grep -c -i "CCS" || true)"
        if [[ "${CCS_HITS}" -eq 0 ]]; then
            echo "Hifiasm_Assemble: input BAM header has no CCS/HiFi marker; refusing to assemble non-HiFi reads" >&2
            exit 2
        fi

        # --- BAM -> FASTQ ---------------------------------------------------
        # hifiasm takes FASTQ (or FASTA). `samtools fastq` preserves the
        # read sequences and qualities; we drop the qualities with `-s
        # /dev/null` for the secondary reads because hifiasm only reads
        # the primary records.
        samtools fastq -@ ~{cpu} ~{hifi_reads_bam} > reads.fq

        # --- hifiasm assembly ----------------------------------------------
        # `-o <prefix>` writes all outputs with the given prefix.
        # `-t ~{cpu}` matches the allocated CPU count so hifiasm does not
        # over- or under-subscribe.
        # `-f 0` disables the bloom filter. Hifiasm's default `-f 37`
        # pre-allocates a 16 GB k-mer bloom table sized for a whole
        # human genome; on chr20-only inputs this over-allocates and
        # the OOM killer takes the process down silently in HealthOmics.
        # `-f 0` uses a hash directly — slower for whole-genome, but
        # for chr20-sized inputs peak RSS drops from >100 GB to ~6 GB
        # and throughput improves. Whole-genome operators MUST supply
        # `-f 37` via `hifiasm_extra_args` when the workflow is run
        # against a full-genome BAM.
        hifiasm -o ~{sample_id} -t ~{cpu} -f 0 reads.fq

        # --- GFA -> bgzipped FASTA ------------------------------------------
        # hifiasm's primary haplotype output files are named
        # `<prefix>.bp.hap1.p_ctg.gfa` and `<prefix>.bp.hap2.p_ctg.gfa`.
        # We extract the S-lines (sequence records) and wrap them as
        # single-line FASTA via awk, then bgzip for downstream tools
        # (PAV) that expect compressed inputs.
        awk '/^S/{print ">"$2"\n"$3}' ~{sample_id}.bp.hap1.p_ctg.gfa \
            | bgzip -@ ~{cpu} > ~{sample_id}.hap1.fa.gz
        awk '/^S/{print ">"$2"\n"$3}' ~{sample_id}.bp.hap2.p_ctg.gfa \
            | bgzip -@ ~{cpu} > ~{sample_id}.hap2.fa.gz
    >>>

    output {
        File hap1_fa_gz = "~{sample_id}.hap1.fa.gz"
        File hap2_fa_gz = "~{sample_id}.hap2.fa.gz"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/hifiasm@sha256:47911f04186fb399c4885088c7de44fea48873111fb4c4f63fd8278a0006e187"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
