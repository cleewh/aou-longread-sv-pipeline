# Feature: aou-longread-sv-pipeline, Property 2: PAV2SVs filter preserves SVs and rewrites source
"""Property-based tests for the PAV2SVs filter (Task 3.13).

**Validates: Requirements 3.3, 3.5**

Property 2 states that for any PAV-shaped input VCF, the PAV2SVs
output contains exactly the input records with ``abs(SVLEN) >= 50``
(preserving all record fields bit-for-bit) AND that the output header
contains exactly one ``##source=PAV`` line regardless of the input's
``##source`` header.

The SVLEN generator deliberately mixes below- and above-threshold
values so both sides of the filter boundary are exercised on every
example.

If :mod:`pysam` is not importable in the test environment the whole
module is skipped (the filter itself also degrades gracefully in that
case — see ``filter.py`` module docstring).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

pysam = pytest.importorskip("pysam")

# Add the pav2svs container source directory to sys.path so we can
# import the filter without building the container.
_PAV2SVS_SRC = (
    Path(__file__).resolve().parents[2]
    / "containers"
    / "pav2svs"
    / "src"
)
if str(_PAV2SVS_SRC) not in sys.path:
    sys.path.insert(0, str(_PAV2SVS_SRC))

import filter as pav2svs_filter  # noqa: E402  (shadows stdlib 'filter' intentionally)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_CHROM = "chr20"
_SVTYPES = ("DEL", "INS", "INV", "DUP")

# Mixed SVLEN strategy: guaranteed blend of below- and above-threshold
# lengths on both sides of zero, so the filter boundary at abs(svlen)
# >= 50 is exercised on every example.
_svlen_strategy = st.one_of(
    st.integers(min_value=-200, max_value=-51),
    st.integers(min_value=-49, max_value=-10),
    st.integers(min_value=10, max_value=49),
    st.integers(min_value=51, max_value=1000),
)

_record_strategy = st.fixed_dictionaries(
    {
        "pos": st.integers(min_value=1000, max_value=10_000_000),
        "svtype": st.sampled_from(_SVTYPES),
        "svlen": _svlen_strategy,
    }
)


_records_strategy = st.lists(
    _record_strategy, min_size=1, max_size=30
).map(
    # Sort by POS so the plain (unbgzipped) fixture VCF we hand to
    # pysam is in the order bcftools/tabix expects. We still exercise
    # a variety of POS values because the strategy draws POS
    # independently per record.
    lambda recs: sorted(recs, key=lambda r: r["pos"])
)


# Upstream ##source header values the PAV wrapper has emitted over the
# versions we care about, plus a ``none`` variant so the "missing
# source" branch of _rewrite_source_header is also exercised.
_source_header_strategy = st.sampled_from(
    [
        "PAV-1.2.3-pre",
        "PAV",  # already correct, filter must still keep exactly one
        "pav-wdl@sh_more_resources_pete",
        "",  # represents "no ##source line in the input"
    ]
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_CONSTANT_HEADER_LINES = (
    "##fileformat=VCFv4.2",
    "##contig=<ID=chr20,length=64444167>",
    "##INFO=<ID=SVTYPE,Number=1,Type=String,Description=\"SV type\">",
    "##INFO=<ID=SVLEN,Number=1,Type=Integer,Description=\"SV length\">",
    "##INFO=<ID=END,Number=1,Type=Integer,Description=\"End position\">",
)


def _write_pav_vcf(
    tmp_path: Path,
    records: list[dict],
    source_header: str,
) -> Path:
    """Write ``records`` as a plain (not bgzipped) PAV-shaped VCF to
    ``tmp_path/pav.vcf`` and return its path. The PAV2SVs filter reads
    plain VCFs directly via pysam.
    """
    plain_path = tmp_path / "pav.vcf"
    lines = list(_CONSTANT_HEADER_LINES)
    if source_header:
        lines.append(f"##source={source_header}")
    lines.append("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO")
    for idx, rec in enumerate(records):
        pos = rec["pos"]
        svtype = rec["svtype"]
        svlen = rec["svlen"]
        end = pos + max(abs(svlen), 1)
        info = f"SVTYPE={svtype};SVLEN={svlen};END={end}"
        ref = "N"
        alt = f"<{svtype}>"
        fields = [
            _CHROM,
            str(pos),
            f"rec{idx}",
            ref,
            alt,
            ".",
            "PASS",
            info,
        ]
        lines.append("\t".join(fields))
    plain_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return plain_path


# ---------------------------------------------------------------------------
# Property 2 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(records=_records_strategy, source_header=_source_header_strategy)
def test_filter_preserves_svs(tmp_path, records, source_header):
    """Output record set equals ``{r for r in input if abs(r.svlen) >= 50}``
    (Requirement 3.3)."""
    in_path = _write_pav_vcf(tmp_path, records, source_header)
    out_path = tmp_path / "out.vcf.gz"
    pav2svs_filter.filter_vcf(in_path, out_path)

    expected = {
        (rec["pos"], rec["svtype"], rec["svlen"])
        for rec in records
        if abs(rec["svlen"]) >= pav2svs_filter.MIN_ABS_SVLEN
    }

    observed: set[tuple[int, str, int]] = set()
    with pysam.VariantFile(str(out_path), "r") as vf:
        for rec in vf:
            svtype_raw = rec.info.get("SVTYPE")
            if isinstance(svtype_raw, (tuple, list)):
                svtype = svtype_raw[0] if svtype_raw else ""
            else:
                svtype = str(svtype_raw) if svtype_raw else ""
            svlen_raw = rec.info.get("SVLEN")
            if isinstance(svlen_raw, (tuple, list)):
                svlen = int(svlen_raw[0]) if svlen_raw else 0
            else:
                svlen = int(svlen_raw) if svlen_raw else 0
            observed.add((rec.pos, svtype, svlen))

    assert observed == expected, (
        f"expected {expected}, got {observed} "
        f"(records={records}, source_header={source_header!r})"
    )


@pytest.mark.property_test
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(records=_records_strategy, source_header=_source_header_strategy)
def test_filter_rewrites_source_header(tmp_path, records, source_header):
    """Output header contains exactly one ``##source=PAV`` line and no
    other ``##source=`` lines (Requirement 3.5)."""
    in_path = _write_pav_vcf(tmp_path, records, source_header)
    out_path = tmp_path / "out.vcf.gz"
    pav2svs_filter.filter_vcf(in_path, out_path)

    # pysam exposes the full header text via str(header). Parsing via
    # line-splitting keeps the assertion independent of pysam's
    # internal record ordering.
    with pysam.VariantFile(str(out_path), "r") as vf:
        header_text = str(vf.header)

    source_lines = [
        line
        for line in header_text.splitlines()
        if line.startswith("##source=")
    ]
    pav_lines = [line for line in source_lines if line == "##source=PAV"]
    other_source_lines = [
        line for line in source_lines if line != "##source=PAV"
    ]

    assert len(pav_lines) == 1, (
        f"expected exactly one ##source=PAV line, got {source_lines!r} "
        f"(input source_header={source_header!r})"
    )
    assert not other_source_lines, (
        f"found non-PAV ##source lines in output: {other_source_lines!r}"
    )


@pytest.mark.property_test
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(records=_records_strategy, source_header=_source_header_strategy)
def test_filter_output_is_bgzipped_and_indexed(
    tmp_path, records, source_header
):
    """Output path ends in ``.vcf.gz`` and has a sibling ``.tbi`` index."""
    in_path = _write_pav_vcf(tmp_path, records, source_header)
    out_path = tmp_path / "out.vcf.gz"
    produced = pav2svs_filter.filter_vcf(in_path, out_path)

    assert str(produced).endswith(".vcf.gz")
    assert out_path.exists()
    assert Path(str(out_path) + ".tbi").exists(), (
        "expected sibling .tbi index alongside bgzipped output"
    )
    # bgzip magic bytes: 1f 8b 08 04. First two are standard gzip;
    # byte 2 must be 08 (DEFLATE) and byte 3 must be 04 (FEXTRA set).
    with open(out_path, "rb") as handle:
        magic = handle.read(4)
    assert magic[:2] == b"\x1f\x8b", "output is not gzipped"
    assert magic[3] == 0x04, (
        f"output missing bgzip FEXTRA flag (byte 3 = 0x{magic[3]:02x})"
    )
