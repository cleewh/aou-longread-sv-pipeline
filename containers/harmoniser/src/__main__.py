"""Entry-point for the harmoniser container.

Usage (inside the container):

    docker run --rm <image> \\
        --pav /in/pav.vcf.gz \\
        --sniffles2 /in/sniffles2.vcf.gz \\
        --pbsv /in/pbsv.vcf.gz \\
        --out /out/harmonised.vcf.gz

At least one of ``--pav`` / ``--sniffles2`` / ``--pbsv`` must be
supplied. If none are supplied, or if every supplied VCF is empty, the
harmoniser exits with code 2 and a Layer 3 error message per
Requirement 6.5.

Requirements: 1.1, 1.2, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
Design: D3, Harmoniser_Task contract
"""

from __future__ import annotations

import sys

from .run_harmoniser import main


if __name__ == "__main__":
    sys.exit(main())
