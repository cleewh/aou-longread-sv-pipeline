# containers/metadata-writer/Dockerfile
#
# Image for the MetadataWriter_Task (Design D8). Bakes the ap-southeast-1
# HealthOmics price list into /opt/pricing/ so Cost_Report computation
# never needs network access (D9) and ships four Python modules under
# /opt/metadata-writer/metadata_writer/ corresponding to the four
# subcommands:
#
#   validate      - validator.py     (Task 3.2, Property 1)
#   compute-cost  - cost_report.py   (Task 3.6, Property 19)
#   write         - writer.py        (Task 3.8, Property 5)
#   (output-namer is a library module only; see output_namer.py)
#
# Build context: this Dockerfile MUST be built from the repository root
# (i.e. the `aou-longread-sv-pipeline/` directory) so that both
# `containers/metadata-writer/src/` and `pricing/healthomics-ap-southeast-1.json`
# are reachable. The presence of a marker file named BUILD_CONTEXT_REPO_ROOT
# next to this Dockerfile signals scripts/mirror-images.py to use the repo
# root as the build context instead of the per-tool directory.
#
# Source layout adaptation: the repo keeps sources under
# `containers/metadata-writer/src/`, but inside the image they are copied
# to `/opt/metadata-writer/metadata_writer/` so that the module name used
# by the ENTRYPOINT (`python -m metadata_writer`) resolves cleanly without
# a separate packaging step.
#
# Multi-arch: built via `docker buildx --platform linux/arm64,linux/amd64`.
# arm64 is the primary target (Requirement 17.5); amd64 is the fallback.
#
# Requirements: 7.2, 12.2, 16.2, 17.10
# Design: D8, D9, MetadataWriter_Task contract

FROM python:3.11-slim

# Fail early on pip / apt errors and keep output unbuffered for HealthOmics
# log collection.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# VCF tooling for header inspection from writer.py (Task 3.8) and
# ca-certificates so boto3's TLS calls work. --no-install-recommends keeps
# the image small; we clean apt lists immediately.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bcftools \
        tabix \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /bin/bash /bin/sh

# Pin to major versions that are known-stable under Python 3.11. jsonschema
# 4.x is the current stable line; boto3 1.34+ ships native HealthOmics
# models for ap-southeast-1.
RUN pip install --no-cache-dir \
        "boto3>=1.34,<2" \
        "jsonschema>=4.21,<5"

# Bake the ap-southeast-1 price list into the image (D9). Build context is
# the repo root, so the path is `pricing/…` rather than `../../pricing/…`.
COPY pricing/healthomics-ap-southeast-1.json /opt/pricing/healthomics-ap-southeast-1.json

# Ship the metadata-writer sources under the `metadata_writer` package name
# so `python -m metadata_writer` resolves via PYTHONPATH below.
COPY containers/metadata-writer/src/ /opt/metadata-writer/metadata_writer/

WORKDIR /opt/metadata-writer

ENV PYTHONPATH=/opt/metadata-writer

ENTRYPOINT ["python", "-m", "metadata_writer"]
# No CMD on purpose: callers supply a subcommand positionally, e.g.
#   docker run --rm <image> validate --manifest /in/manifest.json
