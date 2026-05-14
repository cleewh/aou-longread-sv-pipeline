# Feature: aou-longread-sv-pipeline, Property 19: Cost_Report arithmetic is correct
"""Property-based tests for :mod:`cost_report` (Task 3.7).

**Validates: Requirement 17.10**

Property 19 states that the per-task cost formula and the run-level
total computed by :mod:`cost_report` match the analytic formula within
a 1e-6 floating-point tolerance. The Hypothesis strategies below
generate positive-float resource values plus a synthetic price list so
that the arithmetic is exercised over a broad numerical range without
relying on the (approximate) real HealthOmics rates.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings, strategies as st

import cost_report  # noqa: E402 — sys.path patched by test/conftest.py


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_INSTANCE_NAME = "omics.c.4xlarge"

# Finite, non-NaN positive floats for resource hours. We deliberately cap
# at 1000 hours to keep downstream products in the range where 1e-6
# absolute tolerance comfortably exceeds floating-point rounding error.
_positive_hours = st.floats(
    min_value=0.0,
    max_value=1000.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)

_instance_rate = st.floats(
    min_value=0.01,
    max_value=100.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)

_storage_rate = st.floats(
    min_value=1e-6,
    max_value=1e-3,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)


@st.composite
def price_list_strategy(draw) -> dict:
    """Generate a synthetic ap-southeast-1 price list dict covering a
    single instance type and both storage tiers."""
    return {
        "pricing_source_sha256": "0" * 64,
        "instance_usd_per_hour": {
            _INSTANCE_NAME: draw(_instance_rate),
        },
        "run_storage_usd_per_gb_hour": {
            "DYNAMIC": draw(_storage_rate),
            "STATIC": draw(_storage_rate),
        },
    }


def record_strategy(instance_type: str = _INSTANCE_NAME) -> st.SearchStrategy[dict]:
    """Generate a single task-execution record with positive float
    resource values."""
    return st.fixed_dictionaries(
        {
            "task_name": st.sampled_from(
                ["hifiasm", "pav", "sniffles2", "pbsv", "harmoniser"]
            ),
            "instance_type": st.just(instance_type),
            "cpu_hours": _positive_hours,
            "memory_gb_hours": _positive_hours,
            "storage_gb_hours": _positive_hours,
            "storage_type": st.sampled_from(["DYNAMIC", "STATIC"]),
        }
    )


# ---------------------------------------------------------------------------
# Property 19 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@settings(max_examples=100)
@given(pricing=price_list_strategy(), record=record_strategy())
def test_compute_task_cost_formula(pricing: dict, record: dict) -> None:
    """Per-task cost matches the manually-applied Property 19 formula."""
    rate = pricing["instance_usd_per_hour"][record["instance_type"]]
    storage_rate = pricing["run_storage_usd_per_gb_hour"][record["storage_type"]]
    expected = rate * max(record["cpu_hours"], record["memory_gb_hours"]) + (
        storage_rate * record["storage_gb_hours"]
    )
    observed = cost_report.compute_task_cost(record, pricing)
    assert math.isclose(observed, expected, abs_tol=1e-6), (
        f"expected={expected!r}, observed={observed!r}, record={record!r}"
    )


@pytest.mark.property_test
@settings(max_examples=100)
@given(
    pricing=price_list_strategy(),
    records=st.lists(record_strategy(), min_size=0, max_size=10),
)
def test_compute_total_sums_records(pricing: dict, records: list) -> None:
    """Run-level total equals the sum of per-task estimates."""
    per_task = [cost_report.compute_task_cost(r, pricing) for r in records]
    expected_total = sum(per_task)
    observed_total = cost_report.compute_total(records, pricing)
    # Absolute tolerance alone is too tight for large sums; also accept
    # the equivalent relative tolerance at the same 1e-6 order.
    assert math.isclose(
        observed_total, expected_total, abs_tol=1e-6, rel_tol=1e-6
    ), (
        f"expected_total={expected_total!r}, observed_total={observed_total!r}, "
        f"records={records!r}"
    )


@pytest.mark.property_test
@settings(max_examples=100)
@given(pricing=price_list_strategy(), record=record_strategy())
def test_compute_task_cost_never_negative(pricing: dict, record: dict) -> None:
    """With non-negative inputs the estimated cost is itself non-negative."""
    observed = cost_report.compute_task_cost(record, pricing)
    assert observed >= 0.0, (
        f"expected non-negative cost, got {observed!r} for record={record!r}"
    )
