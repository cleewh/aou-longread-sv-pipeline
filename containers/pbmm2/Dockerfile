# containers/pbmm2/Dockerfile
#
# Image for the pbmm2_Align_Task (Design §pbmm2_Align_Task contract).
# Aligns unaligned HiFi BAMs against a reference FASTA using pbmm2
# (PacBio's minimap2 wrapper), per Requirement 2.2.
#
# The upstream biocontainers image
# `quay.io/biocontainers/pbmm2:1.13.1--h9ee0642_0` is built from the
# same conda recipe as the pbmm2 conda-forge / bioconda package, and
# biocontainers publishes multi-arch (linux/amd64 + linux/arm64)
# manifests for the underlying minimap2 / htslib builds. However, the
# `h9ee0642_0` hash corresponds to an amd64-only build on biocontainers
# at the time of pinning, so we use the same two-stage `ARG TARGETARCH`
# gate as the hifiasm image to handle the arm64 fallback gracefully:
#
#   * amd64 → pulls the pinned biocontainers image verbatim.
#   * arm64 → starts from `condaforge/miniforge3:24.3.0-0` (which IS
#     published multi-arch, including arm64) and installs pbmm2 1.13.1
#     from the bioconda channel. conda-forge resolves the native arm64
#     minimap2 / htslib binaries at install time.
#
# Both paths yield pbmm2 1.13.1; the difference is purely in how the
# binaries arrive.
#
# Requirements: 8.1, 8.3, 17.5
# Design: ECR topology diagram, D5, pbmm2_Align_Task contract

ARG TARGETARCH

# ---- amd64 stage: reuse the pinned biocontainers image verbatim. ----
FROM quay.io/biocontainers/pbmm2:1.13.1--h9ee0642_0 AS amd64-stage

# ---- arm64 stage: install pbmm2 1.13.1 from bioconda onto miniforge3. ----
FROM condaforge/miniforge3:24.3.0-0 AS arm64-stage

# `mamba install -y` resolves an arm64-native build of pbmm2 1.13.1 from
# the bioconda channel; --no-deps is NOT passed because pbmm2 has real
# runtime deps (minimap2, htslib) that must be resolved for the target
# architecture. `conda clean -afy` trims the install size after the
# environment is materialised.
RUN mamba install -y -n base -c bioconda -c conda-forge pbmm2=1.13.1 \
    && mamba clean -afy \
    && find /opt/conda -name '__pycache__' -type d -exec rm -rf {} + || true

# `mamba` activates `base` by default at shell start, but for a single
# ENTRYPOINT invocation we need pbmm2 on PATH directly.
ENV PATH=/opt/conda/bin:$PATH

# ---- final stage: selected by ARG TARGETARCH at buildx time. ----
FROM ${TARGETARCH}-stage AS final

LABEL org.opencontainers.image.title="aou-sv/pbmm2"
LABEL org.opencontainers.image.version="1.13.1"
LABEL org.opencontainers.image.source="https://github.com/PacificBiosciences/pbmm2"

# No CMD / ENTRYPOINT override: both stages put `pbmm2` on PATH. The
# WDL task (Task 6.2) invokes `pbmm2 align ...` directly.
