version 1.1

# wdl/structs.wdl
#
# Task 8.1 support file — shared WDL struct definitions used by both the
# top-level workflow (main.wdl) and the MetadataWriter task
# (tasks/metadata_writer.wdl).
#
# Why a shared file: WDL 1.1 imports propagate struct definitions
# transitively. If `main.wdl` imports `tasks/metadata_writer.wdl` and
# each file defined its own `struct PerCallerStatus`, miniwdl's type
# checker would reject the graph as a duplicate-struct conflict even
# though the two definitions are byte-identical. Extracting the structs
# to `structs.wdl` and importing them from both files is the canonical
# WDL 1.1 workaround.
#
# Both structs are thin typed records — no semantic logic lives here.
# `PerCallerStatus` is the per-run status quartet written into
# `run_metadata.json`; `ToolInfo` is the per-tool (version + image
# digest) record used by MetadataWriter when it assembles the
# `tools` section of the same file. See Design §Data Models for the
# end-to-end shape.
#
# Requirements: 7.2, 11.1, 12.2, 16.2, 17.10
# Design: D8, D9, MetadataWriter_Task contract

# PerCallerStatus — status string for each of the four caller branches
# tracked by the workflow. Values MUST be one of "succeeded" | "failed"
# | "skipped" per the Layer 3 error table (Design §Error Handling).
# writer.py enforces the vocabulary; we keep the struct itself as bare
# String so a future status vocabulary extension is a writer.py change
# alone.
struct PerCallerStatus {
    String hifiasm_pav
    String sniffles2
    String pbsv
    String harmoniser
}

# ToolInfo — one record per tool listed in writer.py's `_REQUIRED_TOOLS`
# (hifiasm, pav, pav2svs, sniffles2, pbsv, pbmm2, harmoniser). The
# MetadataWriter task takes three parallel Array[String] inputs
# (tool_names, tool_versions, tool_digests) and assembles the map
# {name: ToolInfo} at command time; the struct is present here so that
# a future WDL refactor can accept a native `Map[String, ToolInfo]`
# input without changing the downstream writer.py contract.
struct ToolInfo {
    String version
    String image_digest
}
