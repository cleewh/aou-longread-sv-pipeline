# Feature: aou-longread-sv-pipeline, Property 11: Resource override resolution is correct
"""Property-based tests for :mod:`submit_run.resources` (Task 10.4).

**Validates: Requirements 11.2, 11.3**

Property 11: *for any task name and any pair (defaults, override) where
defaults = {cpu, memory_gb, disk_gb} with positive integer values and override
is a partial dictionary of the same shape with positive integer values, the
resolved resource struct merge(defaults, override) SHALL have each field equal
to the override value when present in override and equal to the default value
otherwise, AND every resolved field SHALL be a positive integer.*
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from submit_run.resources import (  # noqa: E402 — sys.path patched by test/conftest.py
    RESOURCE_FIELDS,
    TASK_DEFAULTS,
    merge_overrides,
)


_POSITIVE_INT = st.integers(min_value=1, max_value=1_000_000)


@st.composite
def defaults_and_override(draw):
    """Build ``(defaults, override)`` pairs over ``cpu/memory_gb/disk_gb``.

    ``defaults`` always populates all three fields with positive ints;
    ``override`` picks a random subset (possibly empty, possibly full) of the
    same keys with independent positive int values. This matches the exact
    Property 11 hypothesis shape.
    """
    defaults = {field: draw(_POSITIVE_INT) for field in RESOURCE_FIELDS}
    override_keys = draw(
        st.lists(
            st.sampled_from(RESOURCE_FIELDS),
            min_size=0,
            max_size=len(RESOURCE_FIELDS),
            unique=True,
        )
    )
    override = {key: draw(_POSITIVE_INT) for key in override_keys}
    return defaults, override


@pytest.mark.property_test
@given(defaults_and_override())
@settings(max_examples=100)
def test_merge_override_semantics(data):
    """Override wins when present; default wins otherwise; every field positive."""
    defaults, override = data
    merged = merge_overrides(defaults, override)

    # Structural invariants.
    assert set(merged.keys()) == set(defaults.keys())
    for key, value in merged.items():
        assert isinstance(value, int)
        assert not isinstance(value, bool)
        assert value > 0

    # Field-by-field: override iff present, default otherwise.
    for key in defaults:
        expected = override[key] if key in override else defaults[key]
        assert merged[key] == expected

    # Return value is a fresh dict — mutating it must not affect inputs.
    # Use a sentinel value that cannot collide with any Hypothesis-generated
    # positive int (the strategy caps at 999_999; 10**9 exceeds that).
    sentinel = 10**9
    pre_defaults_cpu = defaults.get("cpu")
    pre_override_cpu = override.get("cpu")
    merged["cpu"] = sentinel
    assert defaults.get("cpu") == pre_defaults_cpu
    assert override.get("cpu") == pre_override_cpu


@pytest.mark.property_test
@given(defaults_and_override())
@settings(max_examples=50)
def test_empty_override_returns_defaults(data):
    """An empty override yields a copy of the defaults dict."""
    defaults, _ = data
    assert merge_overrides(defaults, {}) == dict(defaults)


@pytest.mark.property_test
@given(st.sampled_from(sorted(TASK_DEFAULTS)))
@settings(max_examples=50)
def test_per_task_defaults_are_positive_ints(task_name):
    """Every per-task default in the static table is a positive int for every resource field."""
    defaults = TASK_DEFAULTS[task_name]
    assert set(defaults) == set(RESOURCE_FIELDS)
    for field, value in defaults.items():
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value > 0
