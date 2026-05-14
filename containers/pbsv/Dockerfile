# containers/pbsv/Dockerfile
#
# Image for the PBSV_Discover_Task / PBSV_Merge_Svsig_Task / PBSV_Call_Task
# trio (Design §PBSV branch in task graph). Calls structural variants
# from HiFi-aligned BAMs using PacBio's pbsv tool, per Requirement 5.
#
# amd64-ONLY: pbsv is a PacBio-distributed binary that only ships for
# linux/amd64 as of the 2.9.0 release (biocontainers publishes
# `quay.io/biocontainers/pbsv:2.9.0--h9ee0642_0` with a single-arch
# amd64 manifest, and the upstream PacBio conda recipe has no arm64
# build). This satisfies Requirement 17.6.
#
# Why two-stage: the biocontainers mulled-v2 base is a stripped
# debian:10 rootfs with no package manager — we can't install
# bgzip/tabix on top of it. Stage 1 supplies the pbsv binary (+ its
# bundled libraries) and stage 2 is a debian:12-slim base with htslib
# and samtools from apt. We copy the pbsv binary + its conda-bundled
# shared libraries into stage 2 so pbsv links correctly at runtime.
#
# Requirements: 8.1, 8.3, 17.6
# Design: ECR topology diagram, D5, PBSV branch in task graph

FROM quay.io/biocontainers/pbsv:2.9.0--h9ee0642_0 AS pbsv-src

FROM --platform=linux/amd64 debian:12-slim

# htslib brings `bgzip` + `tabix`; samtools brings `samtools` (useful
# for ad-hoc task debugging); ca-certificates lets any HTTP fetches
# work. pbsv is a statically-linked binary (ldd: libc/libm/libpthread/
# librt only) so we just copy it across.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        libgomp1 \
        samtools \
        tabix \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /bin/bash /bin/sh

COPY --from=pbsv-src /usr/local/bin/pbsv /usr/local/bin/pbsv

# Smoke-test: both pbsv and the htslib tools must run at image-build time.
RUN pbsv --version \
    && bgzip --version | head -1 \
    && tabix --version | head -1

LABEL org.opencontainers.image.title="aou-sv/pbsv"
LABEL org.opencontainers.image.version="2.9.0"
LABEL org.opencontainers.image.source="https://github.com/PacificBiosciences/pbsv"
LABEL org.opencontainers.image.platform.hint="linux/amd64"

# No CMD / ENTRYPOINT override: callers invoke `pbsv ...` / `bgzip ...`
# / `tabix ...` directly from the WDL task command block.
