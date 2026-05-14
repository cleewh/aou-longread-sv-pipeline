version 1.1

# wdl/pav_only.wdl
#
# Standalone PAV-only workflow for iterating on PAV container fixes
# without rebuilding hifiasm every time. Takes pre-made hap1/hap2 FASTAs
# and a reference, invokes PAV_Run directly, and exits.
#
# NOT part of the production pipeline — this file exists purely to
# shorten the PAV debug loop from ~2 hours (hifiasm run) to ~60 min
# (just PAV) per iteration.

import "tasks/pav.wdl" as pav_tasks

workflow aouLongReadSvPavOnly {
    meta {
        version: "0.1.0"
        description: "PAV-only test workflow; consumes pre-built hap FASTAs"
    }

    input {
        File   hap1_fa_gz
        File   hap2_fa_gz
        File   reference_fasta
        File   reference_fai
        String sample_id

        Int?   pav_cpu
        Int?   pav_memory_gb
        Int?   pav_disk_gb
    }

    call pav_tasks.PAV_Run {
        input:
            hap1_fa_gz      = hap1_fa_gz,
            hap2_fa_gz      = hap2_fa_gz,
            reference_fasta = reference_fasta,
            reference_fai   = reference_fai,
            sample_id       = sample_id,
            cpu             = select_first([pav_cpu, 32]),
            memory_gb       = select_first([pav_memory_gb, 128]),
            disk_gb         = select_first([pav_disk_gb, 1000])
    }

    output {
        File pav_sv_vcf = PAV_Run.pav_vcf
    }
}
