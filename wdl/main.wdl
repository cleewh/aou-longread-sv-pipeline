version 1.1

# wdl/main.wdl
#
# Task 8.1 — top-level workflow `aouLongReadSvPipeline`.
#
# This is the single HealthOmics workflow entry point for the AoU
# Long-Read SV Detection Pipeline in ap-southeast-1 (Design D1).
# It wires together the 12 leaf/caller tasks under `tasks/` into the
# graph described in Design §Workflow task graph:
#
#   InputValidator
#     └─ Pbmm2_Align? ─┬─ Hifiasm_Assemble ─ PAV_Run ─ PAV2SVs    (run_hifiasm_pav)
#                      ├─ Sniffles2_Task (scatter) ─ Sniffles2_Merge_Task     (run_sniffles2)
#                      └─ PBSV_Discover_Task (scatter) ─ PBSV_Merge_Svsig_Task ─ PBSV_Call_Task   (run_pbsv)
#   └─ Harmoniser (any non-empty subset of the three SV VCFs)
#        └─ MetadataWriter  (always runs last)
#
# Design-level notes:
#   * Per-caller toggles are honoured with WDL `if (...)` conditionals
#     (Design D1). When every caller is disabled, Harmoniser receives
#     no inputs and its container exits non-zero with
#     "No SV callers produced output" per Design §Layer 3.
#   * The region-residency gate (Design D10) runs in submit-run.py, NOT
#     here. InputValidator is a defence-in-depth schema checker only.
#   * Scatter-over-chromosomes uses a hardcoded autosome + sex-chrom
#     list today; a future task replaces the constant with an output
#     from a `fai_reader` prep task (see note on CHROMS below).
#   * Per-caller status is threaded into MetadataWriter as a
#     PerCallerStatus struct. For v0.1 we optimistically report
#     "succeeded" for any caller that was enabled; Task 19.1 replaces
#     this with real trailer-parsed status.
#   * tool_info metadata is hardcoded as three parallel Array[String]s
#     today; the mirror-images.py pipeline rewrites the digest array
#     before deploy (Task 22).
#
# Requirements: 1.5, 2.1, 2.2, 2.3, 2.6, 3.1, 3.4, 4.1, 4.4, 5.1, 5.4,
#               6.1, 6.5, 7.1, 10.1, 11.1, 16.3, 17.2, 17.8, 17.9, 17.12
# Design: D1, D8, D16

import "structs.wdl"
import "tasks/input_validator.wdl" as iv
import "tasks/pbmm2.wdl" as pbmm2_tasks
import "tasks/hifiasm.wdl" as hifiasm_tasks
import "tasks/pav.wdl" as pav_tasks
import "tasks/pav2svs.wdl" as pav2svs_tasks
import "tasks/sniffles2.wdl" as sniffles2_tasks
import "tasks/pbsv.wdl" as pbsv_tasks
import "tasks/harmoniser.wdl" as harmoniser_tasks
import "tasks/metadata_writer.wdl" as mw_tasks

workflow aouLongReadSvPipeline {
    meta {
        # The version string is kept in sync with the repository's
        # top-level VERSION file per Property 14 (triple consistency
        # between VERSION, main.wdl meta.version, and run_metadata.json
        # pipeline.version). A pre-deploy hook validates the three are
        # equal; updates happen by hand in all three places at release
        # time.
        version: "0.1.0"
        description: "AoU Long-Read SV Detection Pipeline on AWS HealthOmics (ap-southeast-1)"
    }

    input {
        # -------------------------------------------------------------
        # Required per-sample inputs (Design §Input_Manifest, Req 2.1)
        # -------------------------------------------------------------
        String sample_id
        File   hifi_reads_bam
        File?  hifi_reads_bai
        # `hifi_reads_aligned` mirrors the Design field of the same name
        # and drives the optional Pbmm2_Align branch (Req 2.2 / 2.3 /
        # 17.9). When `true`, we skip pbmm2 and reuse the supplied BAM
        # directly; when `false` (the default, matching an unaligned
        # HiFi BAM), pbmm2 runs and its output is used downstream.
        Boolean hifi_reads_aligned = false
        File   reference_fasta
        File   reference_fai
        String output_prefix

        # -------------------------------------------------------------
        # Per-caller toggles (Design §Input_Manifest, Req 2.6)
        # -------------------------------------------------------------
        Boolean run_hifiasm_pav = true
        Boolean run_sniffles2   = true
        Boolean run_pbsv        = true

        # -------------------------------------------------------------
        # Cost / sharding knobs (Req 17.2 / 17.8 / 17.12)
        # -------------------------------------------------------------
        # `shard_by_chromosome` fans Sniffles2 and PBSV discover out
        # per-chromosome using the hardcoded CHROMS list below. Hifiasm
        # and PAV are whole-genome and are NOT sharded (Design D7 / D14).
        Boolean shard_by_chromosome = true
        # `run_storage_type` is passed through to `aws omics start-run
        # --storage-type` by submit-run.py and echoed into
        # run_metadata.json by MetadataWriter. DYNAMIC is the default
        # per Design D6 / Req 17.2.
        String  run_storage_type    = "DYNAMIC"
        # `enable_run_cache` controls whether submit-run.py passes
        # --cache-id when HealthOmics exposes a cache in ap-southeast-1
        # (Design D16 / Req 17.12). The workflow layer only records the
        # flag; the actual cache-id wiring lives in submit-run.py.
        Boolean enable_run_cache    = true

        # -------------------------------------------------------------
        # Harmoniser filter override (Req 6.6)
        # -------------------------------------------------------------
        File?   harmoniser_filter_override_json

        # -------------------------------------------------------------
        # Per-task resource overrides (all optional; Req 11.2 / 11.3)
        # -------------------------------------------------------------
        # Any value left unset falls back to the task's declared default
        # (Design §Resource defaults table, D12). A partial override
        # (e.g. memory_gb only) preserves the other defaults per
        # Property 11. The submit-run.py resource resolver
        # (scripts/submit_run/resources.py, Task 10.3) performs the
        # merge before StartRun; this workflow exposes the per-field
        # knobs so the merged override flows through unchanged.
        Int?    pbmm2_cpu
        Int?    pbmm2_memory_gb
        Int?    pbmm2_disk_gb

        Int?    hifiasm_cpu
        Int?    hifiasm_memory_gb
        Int?    hifiasm_disk_gb

        Int?    pav_cpu
        Int?    pav_memory_gb
        Int?    pav_disk_gb

        Int?    pav2svs_cpu
        Int?    pav2svs_memory_gb
        Int?    pav2svs_disk_gb

        Int?    sniffles2_cpu
        Int?    sniffles2_memory_gb
        Int?    sniffles2_disk_gb

        Int?    sniffles2_merge_cpu
        Int?    sniffles2_merge_memory_gb
        Int?    sniffles2_merge_disk_gb

        Int?    pbsv_discover_cpu
        Int?    pbsv_discover_memory_gb
        Int?    pbsv_discover_disk_gb

        Int?    pbsv_merge_svsig_cpu
        Int?    pbsv_merge_svsig_memory_gb
        Int?    pbsv_merge_svsig_disk_gb

        Int?    pbsv_call_cpu
        Int?    pbsv_call_memory_gb
        Int?    pbsv_call_disk_gb

        Int?    harmoniser_cpu
        Int?    harmoniser_memory_gb
        Int?    harmoniser_disk_gb

        # -------------------------------------------------------------
        # MetadataWriter inputs (Design §run_metadata.json schema)
        # -------------------------------------------------------------
        # `input_manifest_json` is the serialised Input_Manifest echoed
        # back into run_metadata.json. `submit-run.py` writes it to S3
        # before StartRun and passes the URI as this input. The other
        # run-identifying fields (run_id, workflow_id, etc.) are
        # supplied by submit-run.py after StartRun returns; v0.1 accepts
        # placeholder strings from the operator and Task 19.2 replaces
        # them with populated values.
        File   input_manifest_json
        String pipeline_version   = "0.1.0"
        String git_commit         = "unknown"
        String healthomics_run_id = "unknown"
        String workflow_id        = "unknown"
        String workflow_name      = "aou-longread-sv-pipeline"
        String workflow_version   = "0.1.0"
        String run_start_time     = "1970-01-01T00:00:00Z"
        String run_end_time       = "1970-01-01T00:00:00Z"
        String run_status         = "COMPLETED"
    }

    # -----------------------------------------------------------------
    # Autosome + sex-chromosome list for chromosome-level sharding.
    # -----------------------------------------------------------------
    # A hardcoded list keeps the WDL 1.1-compatible v0.1 simple: scatter
    # variables must be known at workflow compile time and WDL 1.1 does
    # not read arbitrary arrays from `.fai` files without a prep task.
    # A future task replaces this constant with an output from a
    # dedicated `fai_reader` task so the shard plan matches whatever
    # reference was passed in. GRCh38 no-alt is the de-facto reference
    # for AoU long-read samples — see Design §Reference_GRCh38 — so the
    # list covers every primary contig of that reference.
    Array[String] CHROMS = [
        "chr1", "chr2", "chr3", "chr4", "chr5", "chr6",
        "chr7", "chr8", "chr9", "chr10", "chr11", "chr12",
        "chr13", "chr14", "chr15", "chr16", "chr17", "chr18",
        "chr19", "chr20", "chr21", "chr22", "chrX", "chrY"
    ]

    # =================================================================
    # 1. InputValidator — workflow-internal schema check.
    # =================================================================
    # Runs on every submission as defence in depth against direct
    # `aws omics start-run` invocations that bypass submit-run.py's
    # client-side residency gate (Design D10). The task writes
    # is_valid/error_message but does NOT itself terminate the
    # workflow; downstream tasks consume the outputs implicitly via
    # task ordering, and v0.1 assumes submit-run.py has already
    # validated the manifest.
    #
    # Task 19.2: the validator's command block also emits the
    # workflow-level start-of-run log line (Req 12.3). We thread
    # `pipeline_version` and `git_commit` through here so the log
    # carries the right values on every run.
    call iv.InputValidator {
        input:
            manifest_json    = input_manifest_json,
            pipeline_version = pipeline_version,
            git_commit       = git_commit
    }

    # =================================================================
    # 2. Alignment (pbmm2) — skipped when an aligned BAM is supplied.
    # =================================================================
    # Req 2.3 / 17.9: when the caller passes `hifi_reads_aligned=true`,
    # we reuse the supplied BAM directly and skip the pbmm2 task. When
    # unaligned, pbmm2 produces the BAM + BAI the read-based callers
    # consume. select_first() resolves to the pbmm2 output when
    # aligned=false and to the input BAM/BAI otherwise.
    if (!hifi_reads_aligned) {
        call pbmm2_tasks.Pbmm2_Align {
            input:
                hifi_reads_bam  = hifi_reads_bam,
                reference_fasta = reference_fasta,
                reference_fai   = reference_fai,
                sample_id       = sample_id,
                cpu             = select_first([pbmm2_cpu, 16]),
                memory_gb       = select_first([pbmm2_memory_gb, 64]),
                disk_gb         = select_first([pbmm2_disk_gb, 500])
        }
    }

    # The downstream read-based callers always consume a single aligned
    # BAM + BAI. When pbmm2 ran we pick its outputs; otherwise we fall
    # back to the supplied inputs. InputValidator rejects the case
    # where aligned=true but hifi_reads_bai is None, so select_first
    # below is safe by construction.
    File aligned_bam = select_first([Pbmm2_Align.aligned_bam, hifi_reads_bam])
    File aligned_bai = select_first([Pbmm2_Align.aligned_bai, hifi_reads_bai])

    # =================================================================
    # 3. Assembly branch — Hifiasm → PAV → PAV2SVs.
    # =================================================================
    if (run_hifiasm_pav) {
        call hifiasm_tasks.Hifiasm_Assemble {
            input:
                hifi_reads_bam = hifi_reads_bam,
                sample_id      = sample_id,
                cpu            = select_first([hifiasm_cpu, 16]),
                memory_gb      = select_first([hifiasm_memory_gb, 32]),
                disk_gb        = select_first([hifiasm_disk_gb, 500])
        }
        call pav_tasks.PAV_Run {
            input:
                hap1_fa_gz      = Hifiasm_Assemble.hap1_fa_gz,
                hap2_fa_gz      = Hifiasm_Assemble.hap2_fa_gz,
                reference_fasta = reference_fasta,
                reference_fai   = reference_fai,
                sample_id       = sample_id,
                cpu             = select_first([pav_cpu, 32]),
                memory_gb       = select_first([pav_memory_gb, 128]),
                disk_gb         = select_first([pav_disk_gb, 1000])
        }
        call pav2svs_tasks.PAV2SVs {
            input:
                pav_vcf   = PAV_Run.pav_vcf,
                sample_id = sample_id,
                cpu       = select_first([pav2svs_cpu, 2]),
                memory_gb = select_first([pav2svs_memory_gb, 8]),
                disk_gb   = select_first([pav2svs_disk_gb, 50])
        }
    }

    # =================================================================
    # 4. Sniffles2 branch — per-chromosome scatter + merge OR whole.
    # =================================================================
    # Design D7: when `shard_by_chromosome=true` we scatter Sniffles2
    # per chromosome and concatenate; when false we run a single
    # whole-sample task. The merge task canonicalises the
    # `##source=Sniffles2` header and bgzips + tabix-indexes the
    # output.
    if (run_sniffles2) {
        if (shard_by_chromosome) {
            scatter (chrom in CHROMS) {
                call sniffles2_tasks.Sniffles2_Task as Sniffles2_Sharded {
                    input:
                        aligned_bam     = aligned_bam,
                        aligned_bai     = aligned_bai,
                        reference_fasta = reference_fasta,
                        reference_fai   = reference_fai,
                        sample_id       = sample_id,
                        region          = chrom,
                        cpu             = select_first([sniffles2_cpu, 8]),
                        memory_gb       = select_first([sniffles2_memory_gb, 32]),
                        disk_gb         = select_first([sniffles2_disk_gb, 200])
                }
            }
            call sniffles2_tasks.Sniffles2_Merge_Task {
                input:
                    shard_vcfs = Sniffles2_Sharded.sv_vcf,
                    sample_id  = sample_id,
                    cpu        = select_first([sniffles2_merge_cpu, 2]),
                    memory_gb  = select_first([sniffles2_merge_memory_gb, 8]),
                    disk_gb    = select_first([sniffles2_merge_disk_gb, 100])
            }
        }
        if (!shard_by_chromosome) {
            # Whole-sample call. When the operator disables sharding we
            # skip the merge task; the single Sniffles2_Whole output is
            # already the per-sample VCF. It is NOT tabix-indexed by
            # Sniffles2 itself, but Sniffles2 writes a `.vcf.gz` either
            # way; the optional tbi output below is provided by the
            # merge task only. v0.1 documents this asymmetry; Task 19
            # unifies the two paths.
            call sniffles2_tasks.Sniffles2_Task as Sniffles2_Whole {
                input:
                    aligned_bam     = aligned_bam,
                    aligned_bai     = aligned_bai,
                    reference_fasta = reference_fasta,
                    reference_fai   = reference_fai,
                    sample_id       = sample_id,
                    region          = "",
                    cpu             = select_first([sniffles2_cpu, 8]),
                    memory_gb       = select_first([sniffles2_memory_gb, 32]),
                    disk_gb         = select_first([sniffles2_disk_gb, 200])
            }
        }
    }

    # Unified Sniffles2 output selector. `if shard_by_chromosome then
    # Sniffles2_Merge_Task.merged_sv_vcf else Sniffles2_Whole.sv_vcf`
    # resolves to the single per-sample Sniffles2 VCF regardless of
    # which branch ran. The outer `if (run_sniffles2)` conditional
    # makes the whole expression optional (File?) as desired.
    File? sniffles2_merged_vcf = if shard_by_chromosome
        then Sniffles2_Merge_Task.merged_sv_vcf
        else Sniffles2_Whole.sv_vcf
    File? sniffles2_merged_vcf_tbi = Sniffles2_Merge_Task.merged_sv_vcf_tbi

    # =================================================================
    # 5. PBSV branch — discover scatter + merge-svsig + call OR whole.
    # =================================================================
    # Design D7: PBSV's `discover` step fans out per chromosome when
    # `shard_by_chromosome=true`; the resulting svsig.gz files are
    # concatenated (bgzip streams concat losslessly) and fed to a
    # single `pbsv call` task. When sharding is disabled, discover runs
    # once over the whole BAM and feeds its single svsig directly into
    # call.
    if (run_pbsv) {
        if (shard_by_chromosome) {
            scatter (chrom in CHROMS) {
                call pbsv_tasks.PBSV_Discover_Task as PBSV_Discover_Sharded {
                    input:
                        aligned_bam = aligned_bam,
                        aligned_bai = aligned_bai,
                        sample_id   = sample_id,
                        region      = chrom,
                        cpu         = select_first([pbsv_discover_cpu, 4]),
                        memory_gb   = select_first([pbsv_discover_memory_gb, 16]),
                        disk_gb     = select_first([pbsv_discover_disk_gb, 200])
                }
            }
            # PBSV_Merge_Svsig_Task removed — pbsv call accepts
            # per-shard svsig files as positional args natively, and
            # `cat`-merging causes duplicate @SQ headers that pbsv
            # rejects with "Different number of chromosomes between
            # svsig and reference".
            call pbsv_tasks.PBSV_Call_Task as PBSV_Call_Sharded {
                input:
                    svsig_shards    = PBSV_Discover_Sharded.svsig_gz,
                    reference_fasta = reference_fasta,
                    reference_fai   = reference_fai,
                    sample_id       = sample_id,
                    cpu             = select_first([pbsv_call_cpu, 8]),
                    memory_gb       = select_first([pbsv_call_memory_gb, 64]),
                    disk_gb         = select_first([pbsv_call_disk_gb, 200])
            }
        }
        if (!shard_by_chromosome) {
            call pbsv_tasks.PBSV_Discover_Task as PBSV_Discover_Whole {
                input:
                    aligned_bam = aligned_bam,
                    aligned_bai = aligned_bai,
                    sample_id   = sample_id,
                    region      = "",
                    cpu         = select_first([pbsv_discover_cpu, 4]),
                    memory_gb   = select_first([pbsv_discover_memory_gb, 16]),
                    disk_gb     = select_first([pbsv_discover_disk_gb, 200])
            }
            call pbsv_tasks.PBSV_Call_Task as PBSV_Call_Whole {
                input:
                    svsig_shards    = [PBSV_Discover_Whole.svsig_gz],
                    reference_fasta = reference_fasta,
                    reference_fai   = reference_fai,
                    sample_id       = sample_id,
                    cpu             = select_first([pbsv_call_cpu, 8]),
                    memory_gb       = select_first([pbsv_call_memory_gb, 64]),
                    disk_gb         = select_first([pbsv_call_disk_gb, 200])
            }
        }
    }

    # Unified PBSV output selector. Same pattern as Sniffles2 above.
    File? pbsv_final_vcf = if shard_by_chromosome
        then PBSV_Call_Sharded.sv_vcf
        else PBSV_Call_Whole.sv_vcf
    File? pbsv_final_vcf_tbi = if shard_by_chromosome
        then PBSV_Call_Sharded.sv_vcf_tbi
        else PBSV_Call_Whole.sv_vcf_tbi

    # =================================================================
    # 6. Harmoniser — merges available per-caller VCFs.
    # =================================================================
    # Harmoniser accepts any non-empty subset of the three per-caller
    # VCFs and fails with "No SV callers produced output" when none
    # are supplied (Design §Layer 3, Req 6.5). WDL optional passthrough
    # means that when a caller's branch was disabled or skipped the
    # corresponding File? evaluates to None and the harmoniser's
    # command block drops the matching CLI flag (see harmoniser.wdl).
    call harmoniser_tasks.Harmoniser {
        input:
            pav_sv_vcf                      = PAV2SVs.sv_vcf,
            sniffles2_sv_vcf                = sniffles2_merged_vcf,
            pbsv_sv_vcf                     = pbsv_final_vcf,
            harmoniser_filter_override_json = harmoniser_filter_override_json,
            sample_id                       = sample_id,
            cpu                             = select_first([harmoniser_cpu, 8]),
            memory_gb                       = select_first([harmoniser_memory_gb, 32]),
            disk_gb                         = select_first([harmoniser_disk_gb, 200])
    }

    # =================================================================
    # 7. MetadataWriter — always the last task (Design D8).
    # =================================================================
    # Per-caller status is optimistically "succeeded" when a caller
    # branch was enabled and "skipped" when it was not.
    #
    # TODO (v0.2): replace this with real stdout-trailer parsing.
    # ----------------------------------------------------------------
    # The Design §Error Handling Layer 3 contract specifies a JSON
    # trailer emitted by every caller container entry point of the
    # form:
    #     {"task": "<name>", "status": "ok"|"error",
    #      "exit_code": N, "stderr_tail": "..."}
    # Task 19.1 landed trailer emission in the four Python modules we
    # own (validator, cost_report, writer, harmoniser, pav2svs.filter)
    # so a future revision can aggregate them via a MetadataWriter
    # pre-step. Emitting trailers from the external tool wrappers
    # (hifiasm, sniffles2, pbsv, pbmm2, pav) requires editing their
    # Dockerfiles to add a shared shell `trap` helper, which is out of
    # scope for v0.1. Until all seven callers emit trailers AND a new
    # WDL parser task consumes them, we keep the optimistic
    # "succeeded"/"skipped" logic here so that run_metadata.json stays
    # well-formed on every completed run.
    PerCallerStatus per_caller_status = PerCallerStatus {
        hifiasm_pav: if run_hifiasm_pav then "succeeded" else "skipped",
        sniffles2:   if run_sniffles2   then "succeeded" else "skipped",
        pbsv:        if run_pbsv        then "succeeded" else "skipped",
        harmoniser:  "succeeded"
    }

    # Tool name / version / digest triple. Names match writer.py's
    # `_REQUIRED_TOOLS`. Versions track the values pinned in
    # containers/manifest.yaml at design time. Digests are sentinel
    # sha256-of-zeros today; mirror-images.py (Task 2.2) rewrites them
    # during the ECR push that precedes Task 22.
    Array[String] tool_names = [
        "hifiasm", "pav", "pav2svs", "sniffles2", "pbsv", "pbmm2", "harmoniser"
    ]
    Array[String] tool_versions = [
        "0.19.9", "2.4.0", "0.1.0", "2.4", "2.9.0", "1.13.1", "0.1.0"
    ]
    Array[String] tool_digests = [
        "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    ]

    # Outputs map echoed into run_metadata.json. Values are the S3
    # URIs HealthOmics localises the produced files to under
    # `<output_prefix>`; MetadataWriter emits them verbatim and never
    # inspects them. The `run_cache_enabled` key threads the
    # `enable_run_cache` flag through to run_metadata.json so an
    # operator can verify the client-side submit-run.py cache wiring
    # matched their request (Req 17.12, Design D16).
    Map[String, String] outputs_map = {
        "harmonised_sv_vcf": output_prefix + sample_id + ".sv.harmonised.vcf.gz",
        "pav_sv_vcf":        output_prefix + sample_id + ".sv.pav.vcf.gz",
        "sniffles2_sv_vcf":  output_prefix + sample_id + ".sv.sniffles2.vcf.gz",
        "pbsv_sv_vcf":       output_prefix + sample_id + ".sv.pbsv.vcf.gz",
        "hifiasm_hap1_fasta": output_prefix + sample_id + ".hap1.fa.gz",
        "hifiasm_hap2_fasta": output_prefix + sample_id + ".hap2.fa.gz",
        "run_cache_enabled":  if enable_run_cache then "true" else "false"
    }

    # Cost records are emitted by each task as a JSON fragment written
    # to the task's stdout trailer (Design §Layer 3). v0.1 has no
    # trailer-reader wired up; MetadataWriter accepts an empty
    # Array[File] here and writes a zero-task Cost_Report. Task 19.1
    # wires the real trailers in.
    Array[File] cost_records = []

    call mw_tasks.MetadataWriter {
        input:
            sample_id           = sample_id,
            pipeline_version    = pipeline_version,
            git_commit          = git_commit,
            input_manifest      = input_manifest_json,
            region              = "ap-southeast-1",
            healthomics_run_id  = healthomics_run_id,
            workflow_id         = workflow_id,
            workflow_name       = workflow_name,
            workflow_version    = workflow_version,
            start_time          = run_start_time,
            end_time            = run_end_time,
            status              = run_status,
            storage_type        = run_storage_type,
            per_caller_status   = per_caller_status,
            tool_names          = tool_names,
            tool_versions       = tool_versions,
            tool_digests        = tool_digests,
            cost_records_json   = cost_records,
            outputs             = outputs_map,
            # File-based dependency on Harmoniser so WDL schedules
            # MetadataWriter AFTER Harmoniser completes (Design D8:
            # MetadataWriter must be the final task). Without this
            # field the empty Array[File] cost_records input would let
            # WDL schedule MetadataWriter concurrently with callers.
            harmonised_sv_vcf_dep = Harmoniser.harmonised_sv_vcf
    }

    # =================================================================
    # 8. Output bundle (Design §Outputs).
    # =================================================================
    # Per-caller VCFs + hap FASTAs are `File?` because their producing
    # branches are conditional. The harmonised VCF and
    # run_metadata.json are required because the workflow only reaches
    # this point if Harmoniser and MetadataWriter succeeded.
    output {
        File  harmonised_sv_vcf     = Harmoniser.harmonised_sv_vcf
        File  harmonised_sv_vcf_tbi = Harmoniser.harmonised_sv_vcf_tbi

        File? pav_sv_vcf     = PAV2SVs.sv_vcf
        File? pav_sv_vcf_tbi = PAV2SVs.sv_vcf_tbi

        File? sniffles2_sv_vcf     = sniffles2_merged_vcf
        File? sniffles2_sv_vcf_tbi = sniffles2_merged_vcf_tbi

        File? pbsv_sv_vcf     = pbsv_final_vcf
        File? pbsv_sv_vcf_tbi = pbsv_final_vcf_tbi

        File? hifiasm_hap1_fasta = Hifiasm_Assemble.hap1_fa_gz
        File? hifiasm_hap2_fasta = Hifiasm_Assemble.hap2_fa_gz

        File  run_metadata_json = MetadataWriter.run_metadata_json

        # Surface the defence-in-depth validator's verdict so an
        # operator reviewing a completed run can see whether the
        # in-workflow schema check agreed with submit-run.py. These
        # outputs are always populated; downstream tooling may assert
        # `input_manifest_valid == true` before trusting the rest of
        # the bundle.
        Boolean input_manifest_valid         = InputValidator.is_valid
        String  input_manifest_error_message = InputValidator.error_message
    }
}
