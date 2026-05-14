# Feature: aou-longread-sv-pipeline, Property 1: Input_Manifest validator is sound and complete
"""Property-based tests for :mod:`validator` (Task 3.3).

**Validates: Requirements 2.1, 2.4, 2.5**

Property 1 states that :func:`validator.validate` returns ``is_valid =
True`` *iff* the Input_Manifest is well-formed (required fields
present and non-empty, sample_id charset, output_prefix trailing slash,
etc.) AND that rejection messages name the offending field. The
individual test functions below cover the soundness direction (a
well-formed manifest passes) and the completeness direction (each
documented mutation produces a rejection that names the offending
field).

All tests run under Hypothesis with ``max_examples=100`` and carry the
``property_test`` marker so that the CI "property tests" job can target
them selectively.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings, strategies as st

import validator  # noqa: E402 — sys.path patched by test/conftest.py


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------


# sample_id: non-empty strings using only [A-Za-z0-9_-], max 32 chars.
# The filter defends against Hypothesis giving us combining marks that
# slip through the whitelist_categories allowance.
valid_sample_id = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="_-",
    ),
    min_size=1,
    max_size=32,
).filter(lambda s: bool(validator.SAMPLE_ID_RE.match(s)))


def valid_s3_uri(ends_with: str = "") -> st.SearchStrategy[str]:
    """Return a strategy for realistic s3:// URIs, optionally constrained
    to end with a given suffix (e.g. ``.bam``, ``/``).
    """
    bucket = st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd"),
                               whitelist_characters="-"),
        min_size=3,
        max_size=20,
    ).filter(lambda s: not s.startswith("-") and not s.endswith("-"))
    path_segment = st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"),
                               whitelist_characters="_-"),
        min_size=1,
        max_size=10,
    )
    # 1..4 path segments
    path = st.lists(path_segment, min_size=1, max_size=4).map(
        lambda parts: "/".join(parts)
    )
    return st.tuples(bucket, path).map(
        lambda bp: f"s3://{bp[0]}/{bp[1]}{ends_with}"
    )


@st.composite
def valid_manifest(draw) -> dict:
    """Build a well-formed Input_Manifest dict."""
    return {
        "sample_id": draw(valid_sample_id),
        "hifi_reads_bam": draw(valid_s3_uri(ends_with=".bam")),
        "reference_fasta": draw(valid_s3_uri(ends_with=".fa")),
        "reference_fai": draw(valid_s3_uri(ends_with=".fa.fai")),
        "output_prefix": draw(valid_s3_uri(ends_with="/")),
        "run_hifiasm_pav": True,
        "run_sniffles2": True,
        "run_pbsv": True,
    }


# Strategy producing at least one character outside [A-Za-z0-9_-].
_invalid_char = st.sampled_from([".", " ", "/", "@", "#", "$", "!", ":"])


# ---------------------------------------------------------------------------
# Property 1 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@settings(max_examples=100)
@given(manifest=valid_manifest())
def test_valid_manifest_passes(manifest: dict) -> None:
    """A manifest with every required field well-formed validates."""
    is_valid, error = validator.validate(manifest)
    assert is_valid is True, (
        f"expected valid manifest to pass, got error={error!r} "
        f"for manifest={manifest!r}"
    )
    assert error == ""


@pytest.mark.property_test
@settings(max_examples=100)
@given(
    manifest=valid_manifest(),
    field=st.sampled_from(validator.REQUIRED_FIELDS),
)
def test_missing_required_field_fails(manifest: dict, field: str) -> None:
    """Removing any required field produces an error naming the field."""
    del manifest[field]
    is_valid, error = validator.validate(manifest)
    assert is_valid is False
    assert field in error, (
        f"expected error to name missing field {field!r}, got {error!r}"
    )


@pytest.mark.property_test
@settings(max_examples=100)
@given(
    manifest=valid_manifest(),
    field=st.sampled_from(validator.REQUIRED_FIELDS),
)
def test_empty_required_field_fails(manifest: dict, field: str) -> None:
    """Setting any required field to the empty string produces an error
    naming the field."""
    manifest[field] = ""
    is_valid, error = validator.validate(manifest)
    assert is_valid is False
    assert field in error


@pytest.mark.property_test
@settings(max_examples=100)
@given(
    manifest=valid_manifest(),
    prefix=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"),
                               whitelist_characters="_-"),
        min_size=0,
        max_size=10,
    ),
    suffix=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"),
                               whitelist_characters="_-"),
        min_size=0,
        max_size=10,
    ),
    bad_char=_invalid_char,
)
def test_bad_sample_id_fails(
    manifest: dict, prefix: str, suffix: str, bad_char: str
) -> None:
    """A sample_id containing at least one character outside
    ``[A-Za-z0-9_-]`` is rejected with a message that names the
    offending character(s) (Requirement 2.5)."""
    sample_id = prefix + bad_char + suffix
    # Ensure we really did introduce a bad character — the prefix/suffix
    # might be empty but bad_char itself is guaranteed invalid.
    assert not validator.SAMPLE_ID_RE.match(sample_id)
    manifest["sample_id"] = sample_id

    is_valid, error = validator.validate(manifest)
    assert is_valid is False
    assert "sample_id" in error
    # The error lists the offending characters; bad_char must appear in
    # the stringified list (wrapped in single quotes by repr()).
    assert repr(bad_char) in error, (
        f"expected offending char {bad_char!r} to appear in {error!r}"
    )


@pytest.mark.property_test
@settings(max_examples=100)
@given(manifest=valid_manifest())
def test_output_prefix_without_slash_fails(manifest: dict) -> None:
    """Stripping the trailing '/' from ``output_prefix`` is rejected with
    a message mentioning ``output_prefix``."""
    manifest["output_prefix"] = manifest["output_prefix"].rstrip("/")
    # After stripping, if the URI collapsed to empty (shouldn't happen
    # given the generator always emits at least "s3://bucket/seg"), the
    # validator would complain about the empty field first. Guard with a
    # fallback so the property stays focused on the trailing-slash rule.
    if manifest["output_prefix"] == "":
        manifest["output_prefix"] = "s3://bucket/path"

    is_valid, error = validator.validate(manifest)
    assert is_valid is False
    assert "output_prefix" in error


@pytest.mark.property_test
@settings(max_examples=100)
@given(manifest=valid_manifest())
def test_all_callers_disabled_fails(manifest: dict) -> None:
    """Setting every ``run_*`` flag to ``False`` is rejected with a
    message mentioning ``callers`` (Requirement 2.6 / 6.5)."""
    manifest["run_hifiasm_pav"] = False
    manifest["run_sniffles2"] = False
    manifest["run_pbsv"] = False

    is_valid, error = validator.validate(manifest)
    assert is_valid is False
    assert "callers" in error
