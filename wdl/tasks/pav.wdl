version 1.1

# wdl/tasks/pav.wdl
#
# Task 7.2 — PAV_Run task (adapted from the `sh_more_resources_pete`
# branch of broadinstitute/pav-wdl).
#
# PAV (https://github.com/EichlerLab/pav) runs a Snakemake-driven
# assembly-vs-reference variant caller. The upstream WDL carries a few
# Terra-only hooks that HealthOmics does not support; this file is the
# HealthOmics-shaped rewrite of that snippet per Design D2 / D14:
#
#   * No `runtime.preemptible`: HealthOmics has no spot/preemptible
#     mode, and the key is unknown to the HealthOmics WDL validator.
#   * No `runtime.bootDiskSizeGb` / `zones`: HealthOmics schedules
#     instances from its own pool and has no GCE-style boot-disk knob.
#   * No `glob(...)` over s3:// URIs: HealthOmics localises all inputs
#     to the task's working directory and does not support direct S3
#     globbing from WDL; we use explicit output paths instead.
#
# PAV is whole-task (not WDL-sharded). Per-contig parallelism is
# handled by Snakemake internally — see Design D14. So this file
# declares a single `PAV_Run` task.
#
# Per-container invocation is delegated to the vendored
# `/opt/pav/run_pav.sh` stub (see containers/pav/src/run_pav.sh),
# which in turn invokes `pav run`. The stub documents the contract so
# the exact `pav run` flag list can be refined once Task 22 (HG002
# chr20) validates the HealthOmics resource settings without us
# needing to rewrite the WDL. Task 7.2 wiring intentionally matches
# the stub's flag names 1:1.
#
# Requirements: 1.1, 1.3, 1.4, 3.2, 3.4, 11.1, 11.3, 17.6
# Design: D2, D14, PAV_Task contract, Resource defaults table
#         (cpu=32, memory=128 GB, disk=1000 GB, amd64-only per
#         Requirement 17.6 — arm64 is unavailable upstream).
#
# ECR image reference — sentinel digest.
# ----------------------------------------
# Tasks 7.* run before Task 22 pushes real images to ECR. Until then the
# `runtime.docker` reference carries a sentinel sha256 of all zeros that
# satisfies Property 6 (every ECR image reference is digest-pinned) and
# keeps the ap-southeast-1 registry host intact. Task 22 / a helper
# script regenerated from `containers/manifest.yaml` rewrites the
# sentinel to the real amd64 digest on first push. PAV is amd64-only
# upstream (RepeatMasker dependency), so there is no arm64 variant.

task PAV_Run {
    input {
        File   hap1_fa_gz
        File   hap2_fa_gz
        File   reference_fasta
        File   reference_fai
        String sample_id
        Int    cpu       = 32
        Int    memory_gb = 128
        Int    disk_gb   = 1000
    }

    command <<<
        set -euxo pipefail

        echo "[PAV_Run] starting at $(date -Is)"
        echo "[PAV_Run] cwd=$(pwd)"
        echo "[PAV_Run] hap1_fa_gz=~{hap1_fa_gz}"
        echo "[PAV_Run] hap2_fa_gz=~{hap2_fa_gz}"
        echo "[PAV_Run] reference_fasta=~{reference_fasta}"
        df -h /tmp . 2>&1 | head -10 || true
        free -m 2>&1 | head || true
        ls -la ~{hap1_fa_gz} ~{hap2_fa_gz} ~{reference_fasta} ~{reference_fai} 2>&1 || true

        # --- Colocate reference + .fai (HealthOmics localises each input
        # to its own folder; PAV / htslib discover the .fai relative to
        # the FASTA path). Same pattern as pbmm2.wdl.
        FASTA_BASENAME="$(basename ~{reference_fasta})"
        ln -sf ~{reference_fasta} "${FASTA_BASENAME}"
        ln -sf ~{reference_fai}   "${FASTA_BASENAME}.fai"
        FASTA_ABS="$(realpath "${FASTA_BASENAME}")"

        # --- Decompress haplotype FASTAs. PAV's Snakemake driver opens
        # the assemblies with a plain FASTA parser and does not handle
        # bgzipped inputs transparently; hifiasm.wdl bgzips them for
        # transport, so we undo that here.
        echo "[PAV_Run] gunzipping haplotype FASTAs at $(date -Is)"
        gunzip -c ~{hap1_fa_gz} > ~{sample_id}.hap1.fa
        gunzip -c ~{hap2_fa_gz} > ~{sample_id}.hap2.fa
        ls -la ~{sample_id}.hap1.fa ~{sample_id}.hap2.fa

        echo "[PAV_Run] invoking run_pav.sh at $(date -Is)"
        # --- Invoke the vendored PAV adapter. The adapter generates
        # config.json + assemblies.tsv and calls PAV's Snakemake
        # pipeline via the container's built-in entrypoint. Pipe
        # through `tee` + `stdbuf` so CloudWatch flushes Snakemake's
        # progress output line-by-line rather than waiting for the
        # whole buffer to fill before emitting.
        stdbuf -oL -eL /opt/pav/run_pav.sh \
            --hap1      "$(realpath ~{sample_id}.hap1.fa)" \
            --hap2      "$(realpath ~{sample_id}.hap2.fa)" \
            --ref       "${FASTA_ABS}" \
            --sample-id ~{sample_id} \
            --out       "$(pwd)/~{sample_id}.pav.all.vcf.gz" \
            --cores     ~{cpu} 2>&1 | stdbuf -oL -eL tee pav_stream.log
        echo "[PAV_Run] done at $(date -Is)"
    >>>

    output {
        # TODO(Task 22): verify the exact output filename emitted by the
        # sh_more_resources_pete branch's `pav run` against the HG002
        # chr20 fixture. The run_pav.sh adapter is written to write to
        # the --out path we pass, so this name holds by construction;
        # the TODO is a belt-and-braces reminder to confirm the end-to
        # -end wiring in Task 22.
        File pav_vcf = "~{sample_id}.pav.all.vcf.gz"
    }

    runtime {
        docker:  "687677765589.dkr.ecr.ap-southeast-1.amazonaws.com/aou-sv/pav@sha256:e36ea9a89aa9370e6336ac64666a2f27b1d3438fd368008d4c53404505b40d07"
        cpu:     cpu
        memory:  "~{memory_gb} GB"
        disks:   "local-disk ~{disk_gb} SSD"
    }
}
