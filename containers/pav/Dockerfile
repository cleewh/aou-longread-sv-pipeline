# containers/pav/Dockerfile
#
# Image for the PAV_Task (Design §PAV_Task contract). Runs PAV
# (https://github.com/EichlerLab/pav) against a pair of haplotype
# assemblies to emit assembly-based variant calls, per Requirement 3.2.
#
# amd64-ONLY: PAV depends on RepeatMasker and associated legacy compiled
# binaries (TRF, DUST, etc.) that only ship for linux/amd64. The upstream
# `becklab/pav:2.4.6` image on Docker Hub (published by the EichlerLab
# PAV maintainers at https://github.com/EichlerLab/pav) ships only
# linux/amd64, and there is no arm64 equivalent. This satisfies
# Requirement 17.6 ("fall back to amd64 when arm64 is unavailable
# upstream"). The arm64 absence is logged in SOURCES.md
# `## Graviton matrix` in the same change that introduced this file.
#
# Because the Graviton matrix in containers/manifest.yaml lists only
# `linux/amd64` for pav, `docker buildx` will never be asked to build
# an arm64 variant of this image. A single FROM line suffices.
#
# Vendored adaptations from the `sh_more_resources_pete` branch of
# broadinstitute/pav-wdl:
#   * Default per-PAV-instance memory bumped (see SOURCES.md under
#     `## Disabled non-essential options` once Task 7.2 wires the
#     precise WDL resource values).
#   * A run_pav.sh adapter script (copied in below) wraps the pav
#     invocation so the WDL task (Task 7.2) can call a single entry
#     point instead of re-assembling the flag list each time. The
#     script is intentionally a thin stub at this point: it accepts
#     the documented flags and invokes `pav run ...`; Task 7.2 will
#     refine the invocation once the exact resource-config override
#     semantics for HealthOmics are nailed down.
#
# Requirements: 1.1, 1.3, 1.4, 8.1, 17.6
# Design: D2, ECR topology diagram, D5, PAV_Task contract

FROM becklab/pav:2.4.6

LABEL org.opencontainers.image.title="aou-sv/pav"
LABEL org.opencontainers.image.version="2.4.6"
LABEL org.opencontainers.image.source="https://github.com/EichlerLab/pav"

# Informational label: this image is amd64-only (see Dockerfile header
# for reasoning). The Graviton matrix static check (Task 15.3) keys off
# this hint when cross-referencing `runtime.docker` strings against
# containers/manifest.yaml's platform list.
LABEL org.opencontainers.image.platform.hint="linux/amd64"

# Ship the run_pav.sh adapter so the WDL task (Task 7.2) can call a
# single entry point. chmod +x so the script is executable regardless
# of the umask under which the mirror script runs the build.
COPY src/run_pav.sh /opt/pav/run_pav.sh
RUN chmod +x /opt/pav/run_pav.sh \
    && ln -sf /bin/bash /bin/sh

# No ENTRYPOINT override: the WDL task (Task 7.2) calls
# `/opt/pav/run_pav.sh ...` directly from its command block, and the
# biocontainers image retains its default shell entry so operators can
# also `docker run --rm -it ... bash` to debug interactively.
