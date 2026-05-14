# Feature: aou-longread-sv-pipeline, Property 20: Cost regression warning and baseline-update semantics
"""Property-based tests for :mod:`cost_regression` (Task 16.5).

**Validates: Requirement 17.11**

Property 20 states that, for any ``(observed_total_usd, baseline_total_usd)``
pair with positive real values, the end-to-end test SHALL emit a
cost-regression warning if and only if ``observed_total_usd >
baseline_total_usd * 1.20``, AND when a warning is emitted,
``test/e2e/cost_baseline.json`` SHALL be updated such that its
``observed_total_usd`` field equals the new observed total. When no
warning fires, the baseline file SHALL remain byte-identical.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from hypothesis import assume, given, settings, strategies as st

# Put ``test/e2e/`` on sys.path so ``import cost_regression`` works as a
# bare top-level module. The module is also accessible via
# ``test.e2e.cost_regression`` because ``test/e2e/__init__.py`` exists;
# the bare-module form is used here to mirror how run_e2e.py imports it.
_E2E_DIR = Path(__file__).resolve().parent.parent / "e2e"
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))

import cost_regression  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MULTIPLIER = 1.20


def _write_baseline(path: Path, observed: float, *, extra: dict | None = None) -> bytes:
    """Write a baseline file and return its exact bytes for later comparison."""
    payload = {
        "observed_at": None,
        "observed_total_usd": float(observed),
        "per_task_usd": {},
        "pipeline_version": "0.1.0",
        "regression_multiplier": _MULTIPLIER,
    }
    if extra:
        payload.update(extra)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return text.encode("utf-8")


# Positive floats bounded to a realistic cost range so ``baseline *
# multiplier`` and ``observed - threshold`` don't drift into
# float-precision weirdness. 1e6 USD is a comfortable ceiling for a
# per-sample HealthOmics run.
_positive_usd = st.floats(
    min_value=0.01,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)


# ---------------------------------------------------------------------------
# Property 20 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@settings(max_examples=100)
@given(observed=_positive_usd)
def test_first_run_seed_no_warning(tmp_path_factory, observed: float) -> None:
    """When baseline.observed_total_usd == 0.0, the call seeds the baseline
    and returns ``(False, seeded_baseline)``.

    The file bytes DO change here (zero → observed) but the warning flag
    is ``False`` because a first-run seed is not a regression.
    """
    # pytest's function-scoped ``tmp_path`` isn't available inside Hypothesis'
    # per-example loop, so we synthesise a unique temp file per example.
    base_dir = tmp_path_factory.mktemp("cost-regression-first")
    baseline_path = base_dir / f"cost_baseline_{hash(observed) & 0xFFFF}.json"
    _write_baseline(baseline_path, 0.0)

    warning_fired, new_baseline = cost_regression.evaluate(observed, baseline_path)

    assert warning_fired is False
    assert new_baseline["observed_total_usd"] == pytest.approx(observed)

    # The file was seeded with the new observed value.
    after = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert after["observed_total_usd"] == pytest.approx(observed)


@pytest.mark.property_test
@settings(max_examples=100, deadline=None)
@given(observed=_positive_usd, baseline=_positive_usd)
def test_under_threshold_no_warning_file_unchanged(
    tmp_path_factory, observed: float, baseline: float
) -> None:
    """observed <= baseline * 1.20 → no warning, file byte-identical."""
    # Constrain the input space to the no-regression case.
    assume(observed <= baseline * _MULTIPLIER)

    base_dir = tmp_path_factory.mktemp("cost-regression-under")
    baseline_path = base_dir / f"cost_baseline_{hash((observed, baseline)) & 0xFFFF}.json"
    pre_bytes = _write_baseline(baseline_path, baseline)

    warning_fired, returned = cost_regression.evaluate(observed, baseline_path)

    assert warning_fired is False
    # Property 20: file must remain byte-identical when no warning fires.
    assert baseline_path.read_bytes() == pre_bytes
    # Returned dict still reflects the on-disk baseline.
    assert returned["observed_total_usd"] == pytest.approx(baseline)


@pytest.mark.property_test
@settings(max_examples=100, deadline=None)
@given(observed=_positive_usd, baseline=_positive_usd)
def test_over_threshold_warns_and_rewrites(
    tmp_path_factory, observed: float, baseline: float
) -> None:
    """observed > baseline * 1.20 → warning, file rewritten with observed."""
    # Constrain to the regression case. baseline must be low enough that
    # our bounded observed can actually exceed ``baseline * 1.20``.
    assume(observed > baseline * _MULTIPLIER)

    base_dir = tmp_path_factory.mktemp("cost-regression-over")
    baseline_path = base_dir / f"cost_baseline_{hash((observed, baseline)) & 0xFFFF}.json"
    pre_bytes = _write_baseline(baseline_path, baseline)

    warning_fired, new_baseline = cost_regression.evaluate(observed, baseline_path)

    assert warning_fired is True
    assert new_baseline["observed_total_usd"] == pytest.approx(observed)
    # File bytes changed — specifically, the observed_total_usd field now
    # equals the observed total.
    after = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert after["observed_total_usd"] == pytest.approx(observed)
    # And the pre-state was different (since baseline != observed within
    # our bounded domain).
    assert baseline_path.read_bytes() != pre_bytes
