# Feature: aou-longread-sv-pipeline, Property 4: Output file names are sample_id-prefixed
"""Property-based tests for :mod:`output_namer` (Task 3.5).

**Validates: Requirement 7.3**

Property 4 states that every basename returned by
:func:`output_namer.expected_output_basenames` starts with
``f"{sample_id}."`` for any valid ``sample_id`` and any per-caller
status dict.

This test also pins down the floor: the three always-emitted files
(harmonised VCF, its tabix index, run_metadata.json) must always be
present regardless of per-caller status — otherwise "every basename is
sample_id-prefixed" would be vacuously true for an empty output list.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

import output_namer  # noqa: E402 — sys.path patched by test/conftest.py
import validator  # noqa: E402 — for the shared SAMPLE_ID_RE


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


valid_sample_id = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="_-",
    ),
    min_size=1,
    max_size=32,
).filter(lambda s: bool(validator.SAMPLE_ID_RE.match(s)))


# Per-caller status dict over the four documented keys, with values
# drawn from {"succeeded", "skipped", "failed"}. Keys may be absent.
_CALLER_KEYS = ("hifiasm_pav", "sniffles2", "pbsv", "harmoniser")
_STATUS_VALUES = ("succeeded", "skipped", "failed")


per_caller_status_strategy = st.fixed_dictionaries(
    mapping={},
    optional={
        key: st.sampled_from(_STATUS_VALUES) for key in _CALLER_KEYS
    },
)


# ---------------------------------------------------------------------------
# Property 4 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@settings(max_examples=100)
@given(sample_id=valid_sample_id, per_caller_status=per_caller_status_strategy)
def test_every_basename_is_sample_id_prefixed(
    sample_id: str, per_caller_status: dict
) -> None:
    """Every basename returned starts with ``f"{sample_id}."``."""
    basenames = output_namer.expected_output_basenames(
        sample_id, per_caller_status
    )
    prefix = f"{sample_id}."
    for name in basenames:
        assert name.startswith(prefix), (
            f"basename {name!r} does not start with {prefix!r} "
            f"(sample_id={sample_id!r}, status={per_caller_status!r})"
        )


@pytest.mark.property_test
@settings(max_examples=100)
@given(sample_id=valid_sample_id, per_caller_status=per_caller_status_strategy)
def test_always_emitted_files_present(
    sample_id: str, per_caller_status: dict
) -> None:
    """Harmonised VCF + tabix + run_metadata.json appear in every output
    regardless of per-caller status."""
    basenames = output_namer.expected_output_basenames(
        sample_id, per_caller_status
    )
    expected_always = {
        f"{sample_id}.sv.harmonised.vcf.gz",
        f"{sample_id}.sv.harmonised.vcf.gz.tbi",
        f"{sample_id}.run_metadata.json",
    }
    assert expected_always.issubset(set(basenames)), (
        f"missing always-emitted basenames from {basenames!r} "
        f"(sample_id={sample_id!r}, status={per_caller_status!r})"
    )
