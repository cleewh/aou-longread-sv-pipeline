# containers/harmoniser/Dockerfile
#
# Image for the Harmoniser_Task (Design D3). Combines per-caller PAV2SVs /
# Sniffles2 / PBSV SV VCFs into a single reconciled VCF with a `CALLERS`
# INFO tag, per Requirement 6.
#
# The upstream `fabio-cunial/callset_integration_phase2` repo is a moving
# target: its scripts change week to week, so we pin it by SHA at mirror
# time. The SHA is recorded in SOURCES.md `## Upstream commits` and
# passed to `docker buildx` as a --build-arg by scripts/mirror-images.py
# (Task 4.6). The Dockerfile defaults to `main` so a one-off developer
# build still works, but production ECR images MUST be built with the
# pinned SHA and the resulting digest MUST round-trip into
# containers/manifest.yaml.
#
# The Python wrapper (run_harmoniser.py) does NOT literally shell out to
# the cloned upstream scripts in this task. The upstream entry points
# are unstable and pinning their exact shape is out of scope for Task
# 3.10. Instead the wrapper implements the documented contract
# (harmonise per-caller SV VCFs, stamp CALLERS, fail-loud on empty
# input) in-process using pysam. The clone is preserved at
# /opt/callset_integration_phase2 for provenance and for a later task
# that can port the exact upstream scripts once they have stabilised.
#
# Multi-arch: built via `docker buildx --platform linux/arm64,linux/amd64`.
# arm64 is the primary target (Requirement 17.5); amd64 is the fallback.
#
# Requirements: 1.1, 1.2, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
# Design: D3, Harmoniser_Task contract

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# bcftools + tabix for VCF I/O fallback; git is needed to clone the
# callset_integration_phase2 repo at build time; ca-certificates is
# required for TLS to github.com.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bcftools \
        tabix \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /bin/bash /bin/sh

# pysam pins to the 0.22+ series because older releases ship a
# pre-built htslib that is incompatible with the bcftools we just
# installed. <1 guards against a future major-version break.
RUN pip install --no-cache-dir "pysam>=0.22,<1"

# Pin the callset_integration_phase2 commit at mirror time. `main` is a
# developer default; scripts/mirror-images.py MUST pass the pinned SHA
# recorded in SOURCES.md via `--build-arg CALLSET_INTEGRATION_PHASE2_SHA=<sha>`.
ARG CALLSET_INTEGRATION_PHASE2_SHA=main

# Clone the upstream scripts for provenance. We wrap in `|| true` so
# that when the SHA placeholder `TBD-pin-at-first-real-mirror` is still
# in SOURCES.md — and the scripts/mirror-images.py has not yet resolved
# it — the build does not fail on an unknown git ref. The Python
# wrapper does not import from this directory (see module docstring for
# run_harmoniser.py), so the clone is strictly documentation. Once the
# SHA is pinned to a real commit this clone must succeed and the `||
# true` becomes a no-op.
RUN git clone https://github.com/fabio-cunial/callset_integration_phase2.git \
        /opt/callset_integration_phase2 \
    && cd /opt/callset_integration_phase2 \
    && (git checkout "${CALLSET_INTEGRATION_PHASE2_SHA}" \
        || [ "${CALLSET_INTEGRATION_PHASE2_SHA}" = "TBD-pin-at-first-real-mirror" ]) \
    || true

# Record the SHA the image was built against so the wrapper can stamp
# the output `##source` header with it at runtime.
ENV CALLSET_INTEGRATION_PHASE2_SHA=${CALLSET_INTEGRATION_PHASE2_SHA}

# Ship the harmoniser sources as the `harmoniser` package.
COPY src/ /opt/harmoniser/harmoniser/

WORKDIR /opt/harmoniser

ENV PYTHONPATH=/opt/harmoniser

ENTRYPOINT ["python", "-m", "harmoniser"]
# No CMD on purpose: callers supply flags positionally, e.g.
#   docker run --rm <image> --pav /in/pav.vcf.gz --out /out/harmonised.vcf.gz
