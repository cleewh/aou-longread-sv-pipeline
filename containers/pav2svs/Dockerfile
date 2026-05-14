# containers/pav2svs/Dockerfile
#
# Image for the PAV2SVs_Task (Design §PAV2SVs_Task contract).
# Filters a PAV variants VCF to retain only structural variants
# (abs(SVLEN) >= 50) and rewrites the VCF `##source` header to `PAV`,
# per Requirement 3.3 and 3.5.
#
# Multi-arch: built via `docker buildx --platform linux/arm64,linux/amd64`.
# arm64 is the primary target (Requirement 17.5); amd64 is the fallback.
#
# Requirements: 3.3, 3.5
# Design: PAV2SVs_Task contract

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# bcftools + tabix provide a subprocess fallback and are useful for
# operators debugging the intermediate VCF. ca-certificates is kept
# for any future HTTPS fetches from within the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bcftools \
        tabix \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /bin/bash /bin/sh

# pysam is the primary VCF I/O implementation (see src/filter.py
# module docstring); versions <1 have a stable API surface for the
# `VariantFile` / `tabix_index` functions we rely on.
RUN pip install --no-cache-dir "pysam>=0.22,<1"

# Ship the pav2svs sources as the `pav2svs` package so
# `python -m pav2svs` resolves via PYTHONPATH below.
COPY src/ /opt/pav2svs/pav2svs/

WORKDIR /opt/pav2svs

ENV PYTHONPATH=/opt/pav2svs

ENTRYPOINT ["python", "-m", "pav2svs"]
# No CMD on purpose: callers supply flags positionally, e.g.
#   docker run --rm <image> --in /in/pav.vcf.gz --out /out/sv.pav.vcf.gz
