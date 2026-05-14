# containers/sniffles2/Dockerfile
#
# Image for the Sniffles2_Task and Sniffles2_Merge_Task (Design §Sniffles2
# branch in task graph). Calls structural variants from HiFi-aligned BAMs
# and merges per-shard VCFs, per Requirement 4.
#
# Sniffles2 (https://github.com/fritzsedlazeck/Sniffles) is a pure-Python
# tool. The pinned biocontainers image
# `quay.io/biocontainers/sniffles:2.4--pyhdfd78af_0` ships a stripped
# debian:12 mulled-v2 rootfs with the sniffles CLI + bundled python3.10
# environment under /usr/local/, but no package manager. The
# Sniffles2_Merge_Task also needs `bcftools concat/sort/view/reheader`
# and `tabix` which are absent. This Dockerfile is two-stage:
#
#   * stage 1 = upstream biocontainers sniffles (the whole /usr/local
#               tree — python3.10 + sniffles package + entry-point).
#   * stage 2 = debian:12-slim + apt install bcftools tabix, then copy
#               the sniffles /usr/local tree across so the sniffles CLI
#               (with its pinned python3.10 interpreter) keeps working.
#
# This layout keeps sniffles binary-compatible with upstream while
# adding the htslib toolchain alongside.
#
# Requirements: 4.1, 4.2, 4.3, 4.4, 8.1, 8.3, 17.5
# Design: ECR topology diagram, D5, Sniffles2 branch in task graph

FROM quay.io/biocontainers/sniffles:2.4--pyhdfd78af_0 AS sniffles-src

FROM debian:12-slim

# tabix ships both `bgzip` and `tabix`. bcftools is needed for
# concat/sort/reheader. ca-certificates covers any HTTP fetches.
# libgomp1 is required by pysam's bundled htslib. We install nothing
# else to keep the final image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bcftools \
        ca-certificates \
        libgomp1 \
        tabix \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /bin/bash /bin/sh

# Pull the sniffles self-contained tree into /opt/sniffles to avoid
# /usr/local/man symlink conflicts with debian:12-slim. Then symlink
# the sniffles entry-point into /usr/local/bin so it's on PATH; the
# interpreter shebang in that script points at /usr/local/bin/python3
# (from the upstream image), so we also need to expose that. Simplest
# way: extend PATH to /opt/sniffles/bin and let the upstream's shebang
# resolve against /opt/sniffles/bin/python3.
COPY --from=sniffles-src /usr/local /opt/sniffles

# The upstream script /opt/sniffles/bin/sniffles starts with
# `#!/usr/local/bin/python3`. Fix it to use its bundled python.
RUN sed -i '1c#!/opt/sniffles/bin/python3' /opt/sniffles/bin/sniffles

ENV PATH=/opt/sniffles/bin:${PATH}

# Smoke-test: all three CLIs must exist and run at build time.
RUN sniffles --version \
    && bcftools --version | head -1 \
    && tabix --version | head -1

LABEL org.opencontainers.image.title="aou-sv/sniffles2"
LABEL org.opencontainers.image.version="2.4"
LABEL org.opencontainers.image.source="https://github.com/fritzsedlazeck/Sniffles"

# No CMD / ENTRYPOINT override: the WDL task invokes `sniffles ...` or
# `bcftools concat ... | bcftools sort ...` directly.
