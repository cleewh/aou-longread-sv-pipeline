#!/bin/bash
# run_pav.sh — WDL-facing adapter for the PAV container.
#
# Generates the config.json + assemblies.tsv that PAV's Snakemake
# pipeline expects, then invokes the container's built-in entrypoint
# script (/opt/pav/files/docker/run) which calls snakemake.
#
# Usage (from the WDL task command block):
#   /opt/pav/run_pav.sh \
#       --hap1 <hap1.fa> --hap2 <hap2.fa> \
#       --ref <reference.fa> \
#       --sample-id <sample> \
#       --out <output.vcf.gz> \
#       --cores <N>
#
# Requirements: 1.1, 1.3, 1.4, 3.2
# Design: D2, D14, PAV_Task contract

set -euo pipefail

HAP1=""
HAP2=""
REF=""
SAMPLE_ID=""
OUT=""
CORES=16

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hap1)      HAP1="$2"; shift 2 ;;
    --hap2)      HAP2="$2"; shift 2 ;;
    --ref)       REF="$2"; shift 2 ;;
    --sample-id) SAMPLE_ID="$2"; shift 2 ;;
    --out)       OUT="$2"; shift 2 ;;
    --cores)     CORES="$2"; shift 2 ;;
    *)
      echo "run_pav.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

# Validate required inputs.
for pair in "HAP1:--hap1" "HAP2:--hap2" "REF:--ref" "SAMPLE_ID:--sample-id" "OUT:--out"; do
  var="${pair%%:*}"
  flag="${pair##*:}"
  if [[ -z "${!var}" ]]; then
    echo "run_pav.sh: missing required flag ${flag}" >&2
    exit 2
  fi
done

# PAV expects to run in a working directory that contains config.json
# and assemblies.tsv. We create a fresh analysis directory and use
# absolute paths for all inputs so PAV's Snakemake resolves them
# regardless of its internal working directory.
ANALYSIS_DIR="$(pwd)/pav_analysis"
mkdir -p "${ANALYSIS_DIR}"

# Resolve all inputs to absolute paths.
HAP1="$(realpath "${HAP1}")"
HAP2="$(realpath "${HAP2}")"
REF="$(realpath "${REF}")"

# Write config.json — minimal: just the reference path.
# PAV docs: "We do not recommend using an assembly with ALTs, decoys,
# or patches" — the caller passes a primary-only reference.
cat > "${ANALYSIS_DIR}/config.json" <<EOF
{
  "reference": "${REF}"
}
EOF

# Write assemblies.tsv — one row per sample.
printf "NAME\tHAP1\tHAP2\n" > "${ANALYSIS_DIR}/assemblies.tsv"
printf "%s\t%s\t%s\n" "${SAMPLE_ID}" "${HAP1}" "${HAP2}" >> "${ANALYSIS_DIR}/assemblies.tsv"

echo "[run_pav.sh] config.json:"
cat "${ANALYSIS_DIR}/config.json"
echo "[run_pav.sh] assemblies.tsv:"
cat "${ANALYSIS_DIR}/assemblies.tsv"
echo "[run_pav.sh] invoking PAV with ${CORES} cores"

# Run PAV via the container's built-in entrypoint script.
# --notemp: keep all intermediate files. PAV's rules have a race
# condition where one rule removes a temp file (e.g.
# contigs_h1.fa.gz.gzi) while another parallel rule still needs it.
# The resulting "No such file or directory" is misreported as
# `call_cigar` failing. Disabling temp-file removal trades a little
# extra disk for correctness.
cd "${ANALYSIS_DIR}"
/opt/pav/files/docker/run -c "${CORES}" -j "${CORES}" --notemp

# PAV writes its output VCF as <analysis_dir>/<sample>.vcf.gz (the
# default target of Snakefile's `pav_all` rule). Copy it to the
# caller-specified --out.
PAV_VCF="${ANALYSIS_DIR}/${SAMPLE_ID}.vcf.gz"
if [[ ! -f "${PAV_VCF}" ]]; then
  echo "run_pav.sh: PAV did not produce expected output at ${PAV_VCF}" >&2
  echo "run_pav.sh: listing ${ANALYSIS_DIR}:" >&2
  find "${ANALYSIS_DIR}" -name "*.vcf*" 2>/dev/null >&2 || true
  exit 3
fi

# Resolve OUT to absolute path relative to the original working dir.
OUT_ABS="$(cd "$(dirname "${OUT}")" 2>/dev/null && pwd)/$(basename "${OUT}")" || OUT_ABS="${OUT}"
cp "${PAV_VCF}" "${OUT_ABS}"
# Also copy the tbi if present — HealthOmics localises both the VCF and
# its index when the downstream harmoniser needs them.
if [[ -f "${PAV_VCF}.tbi" ]]; then
  cp "${PAV_VCF}.tbi" "${OUT_ABS}.tbi"
fi
echo "[run_pav.sh] output: ${OUT_ABS}"
echo '{"task":"pav","status":"ok","exit_code":0,"stderr_tail":""}'
