"""Cost-regression evaluation helper (Task 16.4).

**Validates: Requirement 17.11**
**Implements: Property 20 — Cost regression warning and baseline-update semantics.**

For any ``(observed_total_usd, baseline_total_usd)`` pair with positive real
values, :func:`evaluate` emits a regression warning *if and only if*
``observed_total_usd > baseline_total_usd * regression_multiplier`` (default
1.20), AND when a warning fires, rewrites ``test/e2e/cost_baseline.json``'s
``observed_total_usd`` field to the new observed total. When no warning
fires the baseline file is left byte-identical.

An explicit first-run convention (``baseline.observed_total_usd == 0.0``)
returns ``(False, baseline_with_observed)`` and seeds the baseline file so
subsequent runs have a real number to compare against — this is NOT a
regression warning. The caller can tell the two seed-vs-warn paths apart
by inspecting the returned ``new_baseline`` dict against the pre-call
value, or simply by noting that the returned ``warning_fired`` bool is
``False`` in both cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def evaluate(
    observed: float, baseline_path: str | Path
) -> tuple[bool, dict[str, Any]]:
    """Evaluate the observed cost against the baseline on disk.

    Parameters
    ----------
    observed:
        The run's total USD cost (must be a finite non-negative real number).
    baseline_path:
        Filesystem path to ``test/e2e/cost_baseline.json``. The file is read,
        and rewritten only when a first-run seed or a regression warning
        fires. Otherwise the file bytes are left unchanged.

    Returns
    -------
    (warning_fired, new_baseline)
        ``warning_fired`` is ``True`` iff the observed total exceeded
        ``baseline * regression_multiplier``. ``new_baseline`` is the dict
        that was (or would have been) written when the baseline rotates.

    Property 20 semantics:
      * first run (baseline=0.0): seed without warning; file rewritten with
        the observed total; return ``(False, seeded_baseline)``.
      * observed <= baseline * multiplier: no warning, file untouched;
        return ``(False, unchanged_baseline)``.
      * observed >  baseline * multiplier: warning, file rewritten with the
        observed total; return ``(True, new_baseline)``.
    """
    path = Path(baseline_path)
    baseline = json.loads(path.read_text(encoding="utf-8"))

    current = float(baseline.get("observed_total_usd", 0.0))
    multiplier = float(baseline.get("regression_multiplier", 1.20))

    # First-run seed: replace 0.0 with the observed total and persist.
    if current == 0.0:
        new_baseline = dict(baseline)
        new_baseline["observed_total_usd"] = float(observed)
        _write_baseline(path, new_baseline)
        return False, new_baseline

    threshold = current * multiplier
    if observed > threshold:
        new_baseline = dict(baseline)
        new_baseline["observed_total_usd"] = float(observed)
        _write_baseline(path, new_baseline)
        return True, new_baseline

    # No regression — leave the file byte-identical.
    return False, baseline


def _write_baseline(path: Path, baseline: dict[str, Any]) -> None:
    """Serialise the baseline with a stable layout (sorted keys, 2-space indent, trailing newline).

    Using a stable serialisation means that when the file *does* rewrite,
    the only field that can change in the byte diff is
    ``observed_total_usd`` — satisfying the byte-identical invariant for
    the non-warning path even when the file was originally written with a
    different formatter.
    """
    path.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
