"""Output filename helper (Task 3.4).

Implements Requirements 7.1, 7.3 and the Outputs section of the Design
document. Produces the deterministic list of basenames that a successful
HealthOmics_Run is expected to write under the Input_Manifest's
``output_prefix``.

Property 4 (Task 3.5) asserts that every returned basename starts with
``f"{sample_id}."``.
"""

from __future__ import annotations

#: Status value in ``per_caller_status`` that indicates the caller ran
#: to completion and produced its outputs. Matches Design
#: §run_metadata.json schema.
_SUCCEEDED = "succeeded"

#: Keys used in the ``per_caller_status`` dict. The ``harmoniser`` key
#: is intentionally *not* consulted for per-caller outputs: the three
#: always-emitted files below are always present regardless of
#: harmoniser status (a failed harmoniser fails the whole run before we
#: get here).
_HIFIASM_PAV = "hifiasm_pav"
_SNIFFLES2 = "sniffles2"
_PBSV = "pbsv"


def expected_output_basenames(
    sample_id: str, per_caller_status: dict
) -> list[str]:
    """Return the deterministic list of output file basenames for a run.

    The Design §Outputs section enumerates:

      * Always emitted: harmonised SV VCF (+ tabix), run_metadata.json.
      * Conditional on ``hifiasm_pav`` success: PAV SV VCF (+ tabix),
        two haplotype FASTAs.
      * Conditional on ``sniffles2`` success: Sniffles2 SV VCF (+ tabix).
      * Conditional on ``pbsv`` success: pbsv SV VCF (+ tabix).

    Every returned basename starts with ``f"{sample_id}."``. The list is
    sorted for determinism (Property 4 / Requirement 7.3).

    Parameters
    ----------
    sample_id
        Sample identifier matching ``^[A-Za-z0-9_-]+$`` (validated
        upstream by :mod:`validator`).
    per_caller_status
        Mapping with caller names as keys and status strings as values.
        Any value other than ``"succeeded"`` (including missing keys) is
        treated as "caller did not produce outputs" and suppresses the
        conditional basenames for that caller.
    """
    prefix = f"{sample_id}."
    basenames: list[str] = [
        f"{prefix}sv.harmonised.vcf.gz",
        f"{prefix}sv.harmonised.vcf.gz.tbi",
        f"{prefix}run_metadata.json",
    ]

    if per_caller_status.get(_HIFIASM_PAV) == _SUCCEEDED:
        basenames.extend(
            [
                f"{prefix}sv.pav.vcf.gz",
                f"{prefix}sv.pav.vcf.gz.tbi",
                f"{prefix}hap1.fa.gz",
                f"{prefix}hap2.fa.gz",
            ]
        )
    if per_caller_status.get(_SNIFFLES2) == _SUCCEEDED:
        basenames.extend(
            [
                f"{prefix}sv.sniffles2.vcf.gz",
                f"{prefix}sv.sniffles2.vcf.gz.tbi",
            ]
        )
    if per_caller_status.get(_PBSV) == _SUCCEEDED:
        basenames.extend(
            [
                f"{prefix}sv.pbsv.vcf.gz",
                f"{prefix}sv.pbsv.vcf.gz.tbi",
            ]
        )

    return sorted(basenames)
