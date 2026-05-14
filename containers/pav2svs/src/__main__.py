"""Entry-point for the pav2svs container.

Usage (inside the container):

    docker run --rm <image> --in /in/pav.vcf.gz --out /out/sv.pav.vcf.gz

Emits a bgzipped, tabix-indexed SV VCF derived from the PAV variants
VCF by filtering to ``abs(SVLEN) >= 50`` and rewriting the VCF
``##source`` header line to ``##source=PAV``.

Requirements: 3.3, 3.5
Design: PAV2SVs_Task contract
"""

from __future__ import annotations

import sys

from .filter import main


if __name__ == "__main__":
    sys.exit(main())
