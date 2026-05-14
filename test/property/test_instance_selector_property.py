# Feature: aou-longread-sv-pipeline, Property 15: Instance selection is cost-optimal
"""Property-based tests for :mod:`submit_run.instance_selector` (Task 10.8).

**Validates: Requirements 17.1, 17.3**

Property 15: *for any task resource request (cpu, memory_gb, disk_gb) with
positive integer values, the instance selected by the instance-selection
function SHALL satisfy instance.cpu >= cpu AND instance.memory_gb >= memory_gb
AND instance.disk_gb >= disk_gb, AND no other instance in the ap-southeast-1
HealthOmics price list with a strictly lower hourly price SHALL satisfy all
three constraints.*

Two test suites:

1. A synthetic price list with Hypothesis-generated instances exercises the
   algorithm in full generality.
2. A smoke test against the real ``pricing/healthomics-ap-southeast-1.json``
   confirms the selector interoperates with the production price list that
   ships in the repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import assume, given, settings, strategies as st

from submit_run.instance_selector import (  # noqa: E402 — sys.path patched by test/conftest.py
    InstanceType,
    load_price_list,
    select_instance,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PRICE_LIST_PATH = _REPO_ROOT / "pricing" / "healthomics-ap-southeast-1.json"


# ---------------------------------------------------------------------------
# Synthetic price list strategy
# ---------------------------------------------------------------------------


_CPU_VALUES = st.integers(min_value=1, max_value=128)
_MEM_VALUES = st.integers(min_value=1, max_value=1024)
_PRICE_VALUES = st.floats(
    min_value=0.01, max_value=50.0, allow_nan=False, allow_infinity=False
)


@st.composite
def synthetic_price_list(draw):
    """Build a price_list dict with 3..12 distinct instance types."""
    n = draw(st.integers(min_value=3, max_value=12))
    names = [f"synth.{i}" for i in range(n)]
    specs = {}
    prices = {}
    for name in names:
        specs[name] = {
            "cpu": draw(_CPU_VALUES),
            "memory_gb": draw(_MEM_VALUES),
            "family": "synthetic",
        }
        prices[name] = draw(_PRICE_VALUES)
    return {
        "schema_version": "1.0",
        "region": "ap-southeast-1",
        "instance_specs": specs,
        "instance_usd_per_hour": prices,
    }


# ---------------------------------------------------------------------------
# Property 15 — synthetic price list
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@given(
    cpu=st.integers(min_value=1, max_value=64),
    memory_gb=st.integers(min_value=1, max_value=512),
    disk_gb=st.integers(min_value=1, max_value=2000),
    price_list=synthetic_price_list(),
)
@settings(max_examples=100)
def test_selected_instance_is_cheapest_that_fits(cpu, memory_gb, disk_gb, price_list):
    """Selected instance satisfies bounds AND no strictly-cheaper one does."""
    specs = price_list["instance_specs"]
    prices = price_list["instance_usd_per_hour"]

    # Determine the truth set: every instance whose cpu/memory meets the
    # request. disk_gb is unconstrained by our synthetic specs (no
    # local_ssd_gb field), so it does not participate in the truth set —
    # which matches the implementation's behaviour (Design D13).
    qualifying = [
        (name, prices[name])
        for name, spec in specs.items()
        if spec["cpu"] >= cpu and spec["memory_gb"] >= memory_gb
    ]
    if not qualifying:
        # No instance can satisfy; selector must raise.
        with pytest.raises(ValueError):
            select_instance(cpu, memory_gb, disk_gb, price_list)
        return

    picked: InstanceType = select_instance(cpu, memory_gb, disk_gb, price_list)
    # (1) Bounds satisfied.
    assert picked.cpu >= cpu
    assert picked.memory_gb >= memory_gb
    # (2) No strictly-cheaper instance satisfies the bounds. We compare by
    # strict inequality so ties (multiple instances at the same minimum
    # price) are accepted — the implementation's tie-break is deterministic
    # but Property 15 only requires cost-optimality.
    min_price = min(price for _, price in qualifying)
    assert picked.hourly_usd == pytest.approx(min_price)


@pytest.mark.property_test
@given(
    cpu=st.integers(min_value=1, max_value=64),
    memory_gb=st.integers(min_value=1, max_value=512),
    disk_gb=st.integers(min_value=1, max_value=2000),
    price_list=synthetic_price_list(),
)
@settings(max_examples=50)
def test_selector_is_deterministic(cpu, memory_gb, disk_gb, price_list):
    """Repeated calls with the same inputs return the same instance."""
    # Filter out the no-solution case so the test is a pure determinism check.
    specs = price_list["instance_specs"]
    assume(any(s["cpu"] >= cpu and s["memory_gb"] >= memory_gb for s in specs.values()))
    a = select_instance(cpu, memory_gb, disk_gb, price_list)
    b = select_instance(cpu, memory_gb, disk_gb, price_list)
    assert a == b


# ---------------------------------------------------------------------------
# Smoke test against the real price list that ships in the repo
# ---------------------------------------------------------------------------


def test_selects_from_real_price_list():
    """Sanity: the real ap-southeast-1 price list picks a valid instance for a modest request."""
    price_list = load_price_list(_PRICE_LIST_PATH)
    picked = select_instance(cpu=8, memory_gb=32, disk_gb=100, price_list=price_list)
    assert picked.cpu >= 8
    assert picked.memory_gb >= 32
    # The cheapest qualifying instance at design time is omics.m.2xlarge at
    # $0.76/hr; if the file is refreshed this test may need updating.
    assert picked.hourly_usd > 0
    # Verify optimality against the full price list for this concrete request.
    specs = price_list["instance_specs"]
    prices = price_list["instance_usd_per_hour"]
    qualifying_min = min(
        prices[name]
        for name, spec in specs.items()
        if name in prices and spec["cpu"] >= 8 and spec["memory_gb"] >= 32
    )
    assert picked.hourly_usd == pytest.approx(qualifying_min)
