version 1.1

# wdl/tasks/sniffles2.wdl
#
# Task 7.3 — Sniffles2_Task (per-shard) + Sniffles2_Merge_Task.
#
# Sniffles2 (https://github.com/fritzsedlazeck/Sniffles) is the
# read-based SV caller run against an aligned HiFi BAM. Two tasks live
# in this file because the shard-then-merge topology (Design §Task
# graph) is tightly coupled: the shard task optionally restricts
# calling to a single chromosome when `shard_by_chromosome=true` is set
# on the workflow, and the merge task `bcftools concat`s and sorts the
# per-shard outputs into a single bgzipped, tabix-indexed VCF with a
# `##source=Sniffles2` header.
#
# When `shard_by_chromosome=false`, the workflow calls `Sniffles2_Task`
# once with `region=""` and skips the merge — the single shard VCF is
# already the whole-sample output. When `shard_by_chromosome=true`, the
# workflow scatters over `plan_shards(fai_contents, true)` (see
# scripts/submit_run/shard_planner.py in Task 10.5) and always feeds
# the resulting array through `Sniffles2_Merge_Task`.
#
# Requirements: 4.1, 4.2, 4.3, 4.4, 11.1, 11.3, 17.8
# Design: Sniffles2 branch in task graph, D7, Resource defaults table
#         (Sniffles2_Task: cpu=8, memory=32 GB, disk=200 GB;
#          Sniffles2_Merge_Task: cpu=2, memory=8 GB, disk=100 GB;
#          both arm64 primary).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 7.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real per-platform digest on first push (arm64 primary).

task Sniffles2_Task {
    input {
        File   aligned_bam
        File   aligned_bai
        File   reference_fasta
        File   reference_fai
        String sample_id
        # Empty region ⇒ whole-sample call. Non-empty ⇒ one chromosome
        # per shard (e.g. "chr1"). The workflow layer builds this value
        # from shard_planner.plan_shards() when shard_by_chromosome=true.
        String region    = ""
        Int    cpu       = 8
        Int    memory_gb = 32
        Int    disk_gb   = 200
    }

    # Shard label baked into the per-shard filename. An explicit
    # "unsharded" label keeps the whole-sample case from stomping on
    # the per-chromosome case if an operator accidentally mixes modes.
    String shard_label = if region == "" then "unsharded" else region

    command <<<
        set -euo pipefail

        # Colocate BAM + BAI and FASTA + .fai so htslib / sniffles can
        # discover the indices without relative-path guesswork.
        BAM_BASENAME="$(basename ~{aligned_bam})"
        ln -sf ~{aligned_bam} "${BAM_BASENAME}"
        ln -sf ~{aligned_bai} "${BAM_BASENAME}.bai"

        FASTA_BASENAME="$(basename ~{reference_fasta})"
        ln -sf ~{reference_fasta} "${FASTA_BASENAME}"
        ln -sf ~{reference_fai}   "${FASTA_BASENAME}.fai"

        # Build the regions-file only when region is non-empty. Sniffles2
        # accepts `--regions-file <bed>`; we synthesise a one-line BED
        # covering the whole chromosome (end=300_000_000 safely exceeds
        # any GRCh38 contig length, which the caller will clip to the
        # real contig end).
        REGIONS_FLAG=""
        if [[ -n "~{region}" ]]; then
            printf "%s\t0\t300000000\n" "~{region}" > region.bed
            REGIONS_FLAG="--regions region.bed"
        fi

        # --threads matches allocated CPU so sniffles scales into the
        # task's vCPU budget without over-subscribing.
        sniffles \
            --input     "${BAM_BASENAME}" \
            --reference "${FASTA_BASENAME}" \
            --vcf       ~{sample_id}.~{shard_label}.sniffles2.vcf.gz \
            --threads   ~{cpu} \
            ${REGIONS_FLAG}
    >>>

    output {
        File sv_vcf = "~{sample_id}.~{shard_label}.sniffles2.vcf.gz"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/sniffles2@sha256:25f7c214ff891c74343188c66b10cc77439315c9956c65618cf339a7ef36be60"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}

task Sniffles2_Merge_Task {
    input {
        Array[File] shard_vcfs
        String      sample_id
        Int         cpu       = 2
        Int         memory_gb = 8
        Int         disk_gb   = 100
    }

    command <<<
        set -euo pipefail

        # --- Concatenate + sort the per-shard VCFs. -----------------------
        # `bcftools concat` merges shard VCFs without re-normalising;
        # `bcftools sort` follows because shard boundaries may cross
        # strand- or chromosome-order invariants bcftools relies on.
        bcftools concat -Oz -o concat.vcf.gz ~{sep(" ", shard_vcfs)}
        bcftools sort   -Oz -o sorted.vcf.gz concat.vcf.gz

        # --- Rewrite the `##source=` header line. -------------------------
        # Per Requirement 4.3 the merged VCF carries `##source=Sniffles2`.
        # Sniffles2 itself already emits a source line, but different
        # Sniffles2 releases vary the exact string (e.g.
        # `##source=Sniffles2_2.4`). We canonicalise to the
        # requirement-specified string. `bcftools reheader` rewrites
        # headers without decompressing the record body.
        bcftools view -h sorted.vcf.gz \
            | sed -E 's/^##source=.*$/##source=Sniffles2/' \
            > header.txt
        # Guarantee the header contains the canonical source line even
        # if Sniffles2 emitted no `##source=` at all.
        if ! grep -q '^##source=Sniffles2$' header.txt; then
            # Insert just before the #CHROM column line.
            awk '/^#CHROM/ && !ins { print "##source=Sniffles2"; ins=1 } { print }' \
                header.txt > header.with_source.txt
            mv header.with_source.txt header.txt
        fi
        bcftools reheader -h header.txt sorted.vcf.gz \
            > ~{sample_id}.sv.sniffles2.vcf.gz
        tabix -p vcf ~{sample_id}.sv.sniffles2.vcf.gz
    >>>

    output {
        File merged_sv_vcf     = "~{sample_id}.sv.sniffles2.vcf.gz"
        File merged_sv_vcf_tbi = "~{sample_id}.sv.sniffles2.vcf.gz.tbi"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/sniffles2@sha256:25f7c214ff891c74343188c66b10cc77439315c9956c65618cf339a7ef36be60"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
