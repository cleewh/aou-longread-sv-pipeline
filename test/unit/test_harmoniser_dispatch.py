"""Harmoniser dispatch truth-table test (Task 17.1).

Complements the property-based test in
``test/property/test_harmoniser_property.py`` with deterministic
example coverage of the eight ``(has_pav, has_sniffles2, has_pbsv)``
boolean combinations.

**Validates: Requirements 6.1, 6.4, 6.5**

For each combination:

* ``(False, False, False)`` — the harmoniser wrapper raises
  :class:`run_harmoniser.EmptyHarmoniserInputError` (Requirement 6.5).
* Single-caller cases (one of the three booleans true) — output has at
  least one record and every record's ``CALLERS`` INFO value is exactly
  the one supplied caller (Requirements 6.1, 6.3).
* Two- and three-caller cases with records overlapping within the
  default ``max_position_delta_bp`` (500 bp) — at least one output
  record has ``CALLERS`` containing all supplied callers for that
  cluster (Requirement 6.4).

The synthetic per-caller VCFs follow the same pattern used in the
Hypothesis property test: symbolic ALTs (``<DEL>`` / ``<INS>`` / ...),
one record per shared SV position with small per-caller position
jitter (well under the 500 bp merge radius), SVTYPE + SVLEN + END INFO
declared in the header.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pysam = pytest.importorskip("pysam")

# Make the harmoniser wrapper importable without building the container.
_HARMONISER_SRC = (
    Path(__file__).resolve().parents[2]
    / "containers"
    / "harmoniser"
    / "src"
)
if str(_HARMONISER_SRC) not in sys.path:
    sys.path.insert(0, str(_HARMONISER_SRC))

import run_harmoniser  # noqa: E402


_CHROM = "chr20"

# A deterministic set of four overlapping SV records. Each row is the
# (base_pos, svtype, svlen) triple that every supplied caller will emit
# with small per-caller position jitter — well under the default 500 bp
# merge radius, so clusters reliably span all supplied callers.
_BASE_RECORDS: tuple[tuple[int, str, int], ...] = (
    (1_000_000, "DEL", 200),
    (1_500_000, "INS", 350),
    (2_000_000, "DEL", 1500),
    (2_500_000, "INV", 500),
)

# Per-caller deterministic jitter applied to every base position. Keeps
# records inside the merge radius but not coincident, so the clustering
# path is exercised rather than exact-match dedup.
_CALLER_JITTER = {"PAV": 0, "Sniffles2": 50, "pbsv": -75}


_VCF_HEADER_LINES = (
    "##fileformat=VCFv4.2",
    "##contig=<ID=chr20,length=64444167>",
    "##INFO=<ID=SVTYPE,Number=1,Type=String,Description=\"SV type\">",
    "##INFO=<ID=SVLEN,Number=1,Type=Integer,Description=\"SV length\">",
    "##INFO=<ID=END,Number=1,Type=Integer,Description=\"End position\">",
    "##source=example-caller",
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
)


def _write_caller_vcf(tmp_path: Path, caller: str) -> Path:
    """Write a bgzipped + tabix-indexed synthetic VCF for ``caller``.

    Uses :data:`_BASE_RECORDS` + per-caller jitter to produce records
    that overlap across callers within the default merge radius.
    """
    jitter = _CALLER_JITTER[caller]
    plain_path = tmp_path / f"{caller}.vcf"
    lines = list(_VCF_HEADER_LINES)
    # Sort by jittered POS to satisfy tabix.
    materialised = []
    for idx, (base_pos, svtype, svlen) in enumerate(_BASE_RECORDS):
        pos = max(1, base_pos + jitter)
        materialised.append((pos, idx, svtype, svlen))
    materialised.sort(key=lambda t: t[0])
    for pos, idx, svtype, svlen in materialised:
        end = pos + max(abs(svlen), 1)
        info = f"SVTYPE={svtype};SVLEN={svlen};END={end}"
        lines.append(
            "\t".join(
                [
                    _CHROM,
                    str(pos),
                    f"{caller}_{idx}",
                    "N",
                    f"<{svtype}>",
                    ".",
                    "PASS",
                    info,
                ]
            )
        )
    plain_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pysam.tabix_index(
        str(plain_path), preset="vcf", force=True, keep_original=False
    )
    return Path(str(plain_path) + ".gz")


def _callers_from_info(rec) -> list[str]:
    """Normalise the CALLERS INFO value across pysam representations."""
    raw = rec.info.get("CALLERS")
    if raw is None:
        return []
    if isinstance(raw, (tuple, list)):
        return [c for c in raw if c]
    return [c for c in str(raw).split(",") if c]


# The eight truth-table rows, ordered so the parametrize IDs read as
# binary counts from 0 (no callers) to 7 (all three callers).
_TRUTH_TABLE = [
    (False, False, False),
    (False, False, True),
    (False, True, False),
    (False, True, True),
    (True, False, False),
    (True, False, True),
    (True, True, False),
    (True, True, True),
]


def _provided_set(has_pav: bool, has_sniffles2: bool, has_pbsv: bool) -> set[str]:
    out: set[str] = set()
    if has_pav:
        out.add("PAV")
    if has_sniffles2:
        out.add("Sniffles2")
    if has_pbsv:
        out.add("pbsv")
    return out


@pytest.mark.parametrize("has_pav,has_sniffles2,has_pbsv", _TRUTH_TABLE)
def test_harmoniser_dispatch_truth_table(
    tmp_path, has_pav, has_sniffles2, has_pbsv
):
    """Exhaustively cover the eight ``(has_pav, has_sniffles2, has_pbsv)``
    combinations through the harmoniser wrapper."""
    provided = _provided_set(has_pav, has_sniffles2, has_pbsv)

    paths: dict[str, Path | None] = {
        "PAV": _write_caller_vcf(tmp_path, "PAV") if has_pav else None,
        "Sniffles2": (
            _write_caller_vcf(tmp_path, "Sniffles2") if has_sniffles2 else None
        ),
        "pbsv": _write_caller_vcf(tmp_path, "pbsv") if has_pbsv else None,
    }

    out = tmp_path / "harmonised.vcf.gz"

    # --- empty case: Requirement 6.5 -----------------------------------
    if not provided:
        with pytest.raises(run_harmoniser.EmptyHarmoniserInputError):
            run_harmoniser.harmonise(
                pav=paths["PAV"],
                sniffles2=paths["Sniffles2"],
                pbsv=paths["pbsv"],
                out=out,
            )
        return

    # --- non-empty cases ----------------------------------------------
    run_harmoniser.harmonise(
        pav=paths["PAV"],
        sniffles2=paths["Sniffles2"],
        pbsv=paths["pbsv"],
        out=out,
    )
    assert out.exists(), "harmoniser did not produce an output VCF"

    with pysam.VariantFile(str(out), "r") as vf:
        records = list(vf)
    assert records, "harmoniser produced zero output records"

    callers_per_record = [set(_callers_from_info(r)) for r in records]
    # Every record's CALLERS is a non-empty subset of what we provided
    # (Requirement 6.1 / 6.3).
    for caller_set in callers_per_record:
        assert caller_set, "record had empty CALLERS tag"
        assert caller_set.issubset(provided), (
            f"CALLERS={caller_set} is not a subset of provided {provided}"
        )

    if len(provided) == 1:
        # Single-caller case: every record's CALLERS is exactly the one
        # supplied caller. No way for another caller to appear since
        # only one VCF was supplied.
        only = next(iter(provided))
        for caller_set in callers_per_record:
            assert caller_set == {only}, (
                f"single-caller case {only}: expected {{ {only!r} }}, "
                f"got {caller_set}"
            )
    else:
        # Two- or three-caller case: because every provided caller
        # emits every base record within the default merge radius, at
        # least one output cluster must span all provided callers.
        assert any(
            caller_set == provided for caller_set in callers_per_record
        ), (
            f"expected at least one output record with CALLERS={provided}; "
            f"observed per-record callers: {callers_per_record}"
        )
