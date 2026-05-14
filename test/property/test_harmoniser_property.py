# Feature: aou-longread-sv-pipeline, Property 3: Harmoniser CALLERS tag matches contributing inputs
"""Property-based tests for the harmoniser wrapper (Task 3.11).

**Validates: Requirements 6.1, 6.3, 6.4**

Property 3 states that for any non-empty subset of per-caller SV VCFs
supplied to the Harmoniser_Task, every record in the harmonised output
carries a ``CALLERS`` INFO tag whose value is a non-empty subset of the
provided callers, AND each listed caller has a matching source record
within the harmoniser's documented merge radius.

The fixture construction (bgzipping synthetic VCFs on every
invocation) makes these tests more expensive than pure-function
property tests, so ``max_examples`` is capped at 50 rather than the
100 used elsewhere.

If :mod:`pysam` is not importable in the test environment the whole
module is skipped (the wrapper itself also degrades gracefully in that
case — see ``run_harmoniser.py`` module docstring).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

pysam = pytest.importorskip("pysam")

# Add the harmoniser container source directory to sys.path so we can
# import the wrapper without building the container. This mirrors the
# sys.path trick in test/conftest.py for the metadata-writer sources.
_HARMONISER_SRC = (
    Path(__file__).resolve().parents[2]
    / "containers"
    / "harmoniser"
    / "src"
)
if str(_HARMONISER_SRC) not in sys.path:
    sys.path.insert(0, str(_HARMONISER_SRC))

import run_harmoniser  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_CALLERS = ("PAV", "Sniffles2", "pbsv")
_CHROM = "chr20"

# SV types we generate. Limited to the documented set used by all three
# callers; anything more exotic is filtered out by PAV2SVs / pbsv / Sniffles2
# before reaching the harmoniser.
_SVTYPES = ("DEL", "INS", "INV", "DUP")


_record_strategy = st.fixed_dictionaries(
    {
        "pos": st.integers(min_value=1000, max_value=10_000_000),
        "svtype": st.sampled_from(_SVTYPES),
        "svlen": st.integers(min_value=50, max_value=50_000),
    }
)


def _base_record_list() -> st.SearchStrategy[list[dict]]:
    """Strategy: up to 20 base SV records. May be empty so the "empty
    input" property test can use the same strategy."""
    return st.lists(_record_strategy, min_size=0, max_size=20)


def _caller_subset() -> st.SearchStrategy[tuple[str, ...]]:
    """Non-empty subsets of the three callers, stable order."""
    return st.lists(
        st.sampled_from(_CALLERS), min_size=1, max_size=3, unique=True
    ).map(lambda xs: tuple(c for c in _CALLERS if c in xs))


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_VCF_HEADER_LINES = (
    "##fileformat=VCFv4.2",
    "##contig=<ID=chr20,length=64444167>",
    "##INFO=<ID=SVTYPE,Number=1,Type=String,Description=\"SV type\">",
    "##INFO=<ID=SVLEN,Number=1,Type=Integer,Description=\"SV length\">",
    "##INFO=<ID=END,Number=1,Type=Integer,Description=\"End position\">",
    "##source=example-caller",
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
)


def _write_caller_vcf(
    tmp_path: Path,
    caller: str,
    records: list[dict],
    jitter_map: dict[int, int] | None = None,
) -> Path:
    """Write ``records`` (possibly with per-record positional jitter) to
    a bgzipped VCF under ``tmp_path/<caller>.vcf.gz`` and tabix-index it.

    ``jitter_map`` is an optional ``{record_idx: delta_bp}`` mapping used
    by the caller-overlap strategy to simulate the same underlying SV
    being called at slightly different positions across tools.
    """
    jitter_map = jitter_map or {}
    plain_path = tmp_path / f"{caller}.vcf"
    lines = list(_VCF_HEADER_LINES)
    # Materialise records with their jittered POS then sort by POS so
    # that tabix_index does not reject the file as unsorted. Preserving
    # the original record index in the ID keeps the record identifiable
    # across the jitter transform.
    materialised: list[tuple[int, dict, int]] = []
    for idx, rec in enumerate(records):
        pos = max(1, rec["pos"] + jitter_map.get(idx, 0))
        materialised.append((pos, rec, idx))
    materialised.sort(key=lambda t: t[0])
    for pos, rec, idx in materialised:
        svtype = rec["svtype"]
        svlen = rec["svlen"]
        end = pos + max(abs(svlen), 1)
        info = f"SVTYPE={svtype};SVLEN={svlen};END={end}"
        # Use a symbolic ALT so pysam reliably writes the record without
        # worrying about DEL needing a REF spanning the deletion.
        ref = "N"
        alt = f"<{svtype}>"
        fields = [
            _CHROM,
            str(pos),
            f"{caller}_{idx}",
            ref,
            alt,
            ".",
            "PASS",
            info,
        ]
        lines.append("\t".join(fields))
    plain_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # bgzip + tabix-index via pysam so the wrapper can read it.
    pysam.tabix_index(
        str(plain_path), preset="vcf", force=True, keep_original=False
    )
    return Path(str(plain_path) + ".gz")


# ---------------------------------------------------------------------------
# Property 3 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    base_records=_base_record_list(),
    provided_callers=_caller_subset(),
    jitters=st.lists(
        st.integers(min_value=-250, max_value=250), min_size=0, max_size=20
    ),
)
def test_every_record_has_callers_tag(
    tmp_path, base_records, provided_callers, jitters
):
    """Every harmonised output record carries a non-empty ``CALLERS``
    INFO value (Requirement 6.3)."""
    if not base_records:
        # Degenerate strategy outcome; the "empty input" test below
        # covers this case. Skip quietly here to keep this property
        # focused on non-empty runs.
        return

    # For each provided caller, write a jittered copy of the base records.
    paths: dict[str, Path] = {}
    for offset, caller in enumerate(provided_callers):
        jitter_map = {
            idx: jitters[(idx + offset) % len(jitters)]
            for idx in range(len(base_records))
            if jitters
        }
        paths[caller] = _write_caller_vcf(
            tmp_path, caller, base_records, jitter_map
        )

    out = tmp_path / "harmonised.vcf.gz"
    try:
        run_harmoniser.harmonise(
            pav=paths.get("PAV"),
            sniffles2=paths.get("Sniffles2"),
            pbsv=paths.get("pbsv"),
            out=out,
        )
    except run_harmoniser.EmptyHarmoniserInputError:
        # Possible if every record was filtered by abs(SVLEN) >= 50.
        # Since our strategy enforces svlen >= 50 this should never
        # happen with non-empty base_records; fail loudly so we don't
        # silently skip inputs we intended to test.
        pytest.fail(
            "harmoniser treated non-empty inputs as empty — strategy bug"
        )

    assert out.exists()
    with pysam.VariantFile(str(out), "r") as vf:
        records = list(vf)
        assert records, "harmoniser produced zero output records"
        for rec in records:
            callers_raw = rec.info.get("CALLERS")
            assert callers_raw is not None, (
                f"record at {rec.chrom}:{rec.pos} has no CALLERS tag"
            )
            # pysam returns CSV-as-tuple for Number=. String INFOs.
            if isinstance(callers_raw, (tuple, list)):
                callers = [c for c in callers_raw if c]
            else:
                callers = [c for c in str(callers_raw).split(",") if c]
            assert callers, (
                f"record at {rec.chrom}:{rec.pos} has empty CALLERS tag"
            )


@pytest.mark.property_test
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    base_records=_base_record_list(),
    provided_callers=_caller_subset(),
    jitters=st.lists(
        st.integers(min_value=-250, max_value=250), min_size=0, max_size=20
    ),
)
def test_callers_is_subset_of_provided(
    tmp_path, base_records, provided_callers, jitters
):
    """``CALLERS`` values are a subset of the callers whose VCFs were
    provided, AND each listed caller has a source record within the
    harmoniser's merge radius (Requirements 6.1, 6.4)."""
    if not base_records:
        return

    # Build per-caller VCFs with jitter so overlap is realistic but
    # bounded (<=250 bp << default 500 bp merge radius).
    paths: dict[str, Path] = {}
    per_caller_records: dict[str, list[dict]] = {}
    for offset, caller in enumerate(provided_callers):
        jitter_map = {
            idx: jitters[(idx + offset) % len(jitters)]
            for idx in range(len(base_records))
            if jitters
        }
        paths[caller] = _write_caller_vcf(
            tmp_path, caller, base_records, jitter_map
        )
        # Record each caller's actual positions so we can assert the
        # merge-radius invariant downstream.
        per_caller_records[caller] = [
            {
                "pos": max(1, rec["pos"] + jitter_map.get(idx, 0)),
                "svtype": rec["svtype"],
                "svlen": rec["svlen"],
            }
            for idx, rec in enumerate(base_records)
        ]

    out = tmp_path / "harmonised.vcf.gz"
    run_harmoniser.harmonise(
        pav=paths.get("PAV"),
        sniffles2=paths.get("Sniffles2"),
        pbsv=paths.get("pbsv"),
        out=out,
    )

    provided_set = set(provided_callers)
    merge_radius = run_harmoniser.DEFAULT_MAX_POSITION_DELTA_BP
    svlen_frac = run_harmoniser.DEFAULT_MAX_SVLEN_FRACTION

    with pysam.VariantFile(str(out), "r") as vf:
        for rec in vf:
            callers_raw = rec.info.get("CALLERS")
            if isinstance(callers_raw, (tuple, list)):
                callers = [c for c in callers_raw if c]
            else:
                callers = [c for c in str(callers_raw).split(",") if c]
            # Subset invariant (Req 6.1 / 6.3).
            assert set(callers).issubset(provided_set), (
                f"CALLERS={callers} not a subset of provided {provided_set}"
            )
            # Merge-radius invariant (Req 6.4): every listed caller must
            # have a record on the same chrom + svtype with position
            # within merge_radius AND svlen within svlen_frac of the
            # representative.
            rep_svlen_raw = rec.info.get("SVLEN")
            if isinstance(rep_svlen_raw, (tuple, list)):
                rep_svlen = int(rep_svlen_raw[0]) if rep_svlen_raw else 0
            else:
                rep_svlen = int(rep_svlen_raw) if rep_svlen_raw else 0
            rep_svtype_raw = rec.info.get("SVTYPE")
            if isinstance(rep_svtype_raw, (tuple, list)):
                rep_svtype = rep_svtype_raw[0] if rep_svtype_raw else ""
            else:
                rep_svtype = str(rep_svtype_raw) if rep_svtype_raw else ""
            for caller in callers:
                source = per_caller_records.get(caller, [])
                found = False
                for src in source:
                    if src["svtype"] != rep_svtype:
                        continue
                    if abs(src["pos"] - rec.pos) > merge_radius:
                        continue
                    denom = max(abs(src["svlen"]), abs(rep_svlen))
                    if denom == 0:
                        if abs(src["svlen"]) == abs(rep_svlen):
                            found = True
                            break
                    elif (
                        abs(abs(src["svlen"]) - abs(rep_svlen))
                        <= svlen_frac * denom
                    ):
                        found = True
                        break
                assert found, (
                    f"caller {caller} listed in CALLERS but no source "
                    f"record within merge radius for "
                    f"{rec.chrom}:{rec.pos} {rep_svtype} svlen={rep_svlen}"
                )


@pytest.mark.property_test
@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(dummy=st.integers(min_value=0, max_value=3))
def test_empty_inputs_fails(tmp_path, dummy):
    """Calling the harmoniser wrapper with zero VCFs raises
    :class:`EmptyHarmoniserInputError` (Requirement 6.5)."""
    out = tmp_path / "harmonised.vcf.gz"
    with pytest.raises(run_harmoniser.EmptyHarmoniserInputError):
        run_harmoniser.harmonise(pav=None, sniffles2=None, pbsv=None, out=out)
