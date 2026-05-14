# containers/hifiasm/Dockerfile
#
# Image for the Hifiasm_Task (Design §Hifiasm_Task contract). Assembles
# HiFi long reads into a diploid pair of haplotype FASTAs, per
# Requirement 3.1 / 3.4.
#
# amd64-ONLY. Hifiasm 0.19.9 pervasively depends on x86 SSE2 and SSE4.2
# intrinsics — it #includes `emmintrin.h` directly in core translation
# units (Levenshtein_distance.h, etc.) and passes `-msse4.2 -mpopcnt`
# in its Makefile. A clean arm64 port would require pulling in
# sse2neon + dealing with pop-count and string-search intrinsics by
# hand; that carries non-trivial numerical-correctness risk on a tool
# used as the primary assembler for an SV pipeline. Hifiasm upstream
# does not ship an arm64 build and biocontainers publishes only an
# amd64 manifest, so this Dockerfile mirrors that reality: the amd64
# HealthOmics instance families are used for the Hifiasm_Task, and the
# Graviton matrix in SOURCES.md is updated accordingly.
#
# The biocontainers mulled-v2 image
# `quay.io/biocontainers/hifiasm:0.19.9--h43eeafb_0` is a stripped
# rootfs that contains only the hifiasm binary (no mamba, no conda, no
# samtools). Since the Hifiasm_Task also needs samtools (for the
# @PG/CCS header check and for `samtools fastq` BAM->FASTQ
# conversion), we compile from source on debian:12-slim which ships a
# ready samtools in apt. This yields an amd64 image that is
# self-contained for the WDL task's needs.
#
# Requirements: 8.1, 8.3, 17.6 (amd64-only fallback)
# Design: ECR topology diagram, D5, Hifiasm_Task contract

FROM --platform=linux/amd64 debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        git \
        samtools \
        tabix \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /bin/bash /bin/sh

# Compile the pinned 0.19.9 tag. `--depth 1 --branch 0.19.9` keeps the
# clone small and guarantees the exact tag without fetching unrelated
# history.
RUN git clone --depth 1 --branch 0.19.9 \
        https://github.com/chhylp123/hifiasm.git /opt/hifiasm \
    && make -C /opt/hifiasm

ENV PATH=/opt/hifiasm:$PATH

LABEL org.opencontainers.image.title="aou-sv/hifiasm"
LABEL org.opencontainers.image.version="0.19.9"
LABEL org.opencontainers.image.source="https://github.com/chhylp123/hifiasm"
LABEL org.opencontainers.image.platform.hint="linux/amd64"

# No CMD / ENTRYPOINT override: `/opt/hifiasm` is on PATH above, so
# callers invoke `hifiasm ...` directly from the WDL task command block
# (Task 7.1). samtools is on the default PATH via the apt install.
