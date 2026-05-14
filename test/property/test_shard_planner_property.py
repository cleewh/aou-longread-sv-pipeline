# Feature: aou-longread-sv-pipeline, Property 18: Chromosome shard plan is complete and non-overlapping
"""Property-based tests for :mod:`submit_run.shard_planner` (Task 10.6).

**Validates: Requirement 17.8**

Property 18: *for any reference FAI file listing contigs C = {c_1, …, c_n}
and any boolean shard_by_chromosome, the shard plan SHALL satisfy: when
shard_by_chromosome is true, the plan contains exactly n shards, one per
contig, and the union of the shard regions equals the full reference; when
shard_by_chromosome is false, the plan contains exactly one shard covering
the full reference.*
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from submit_run.shard_planner import (  # noqa: E402 — sys.path patched by test/conftest.py
    WHOLE_REFERENCE_CONTIG,
    Region,
    plan_shards,
)


# .fai contig names use a subset of printable characters that will not clash
# with the tab-separated FAI format. Keep the alphabet realistic (letters,
# digits, underscore, dot, dash) so we don't spend Hypothesis budget on
# unicode edge cases the real tools never produce.
_CONTIG_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"

contig_name = st.text(
    alphabet=_CONTIG_NAME_ALPHABET, min_size=1, max_size=20
).filter(lambda s: not s.startswith("#"))


@st.composite
def fai_contents(draw):
    """Build an FAI blob listing 1..20 contigs each with a positive length.

    Returned as ``(fai_str, [(contig, length), ...])`` so tests can compare
    against the truth directly without reparsing the FAI.
    """
    count = draw(st.integers(min_value=1, max_value=20))
    names = draw(
        st.lists(
            contig_name, min_size=count, max_size=count, unique=True
        )
    )
    lengths = draw(
        st.lists(
            st.integers(min_value=1, max_value=300_000_000),
            min_size=count,
            max_size=count,
        )
    )
    # Build a valid FAI: ``name<TAB>length<TAB>offset<TAB>linebases<TAB>linewidth``.
    offset = 0
    lines = []
    for name, length in zip(names, lengths):
        # linebases=60, linewidth=61 is the htslib default; values don't
        # matter for shard planning but keep the blob realistic.
        lines.append(f"{name}\t{length}\t{offset}\t60\t61")
        offset += length + length // 60 + 1
    return "\n".join(lines) + "\n", list(zip(names, lengths))


@pytest.mark.property_test
@given(fai_contents())
@settings(max_examples=100)
def test_per_chromosome_shards_cover_full_reference(data):
    """shard_by_chromosome=True → exactly n shards; each covers its contig end-to-end."""
    fai_str, truth = data
    regions = plan_shards(fai_str, shard_by_chromosome=True)
    assert len(regions) == len(truth)
    for region, (name, length) in zip(regions, truth):
        assert isinstance(region, Region)
        assert region.contig == name
        assert region.start == 0
        assert region.end == length
    # Union check: total bases covered equals sum of contig lengths.
    total_covered = sum(r.length for r in regions)
    assert total_covered == sum(length for _, length in truth)


@pytest.mark.property_test
@given(fai_contents())
@settings(max_examples=100)
def test_whole_reference_when_sharding_disabled(data):
    """shard_by_chromosome=False → exactly one WHOLE-reference shard."""
    fai_str, truth = data
    regions = plan_shards(fai_str, shard_by_chromosome=False)
    assert len(regions) == 1
    only = regions[0]
    assert only.contig == WHOLE_REFERENCE_CONTIG
    assert only.start == 0
    assert only.end == sum(length for _, length in truth)


@pytest.mark.property_test
@given(fai_contents())
@settings(max_examples=50)
def test_shards_non_overlapping_and_unique_per_contig(data):
    """Per-chromosome shards are on distinct contigs (non-overlap trivially holds)."""
    fai_str, _ = data
    regions = plan_shards(fai_str, shard_by_chromosome=True)
    contigs = [r.contig for r in regions]
    assert len(contigs) == len(set(contigs))
