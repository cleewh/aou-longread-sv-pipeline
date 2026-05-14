# Task 10.3: Per-task resource-override merger for submit-run.py.
"""Resource override resolver for HealthOmics submit-run.

Requirements: 11.2, 11.3
Design: Resource defaults table, Property 11.

Property 11 defines the merge contract: *for any task name and any pair
(defaults, override) where defaults = {cpu, memory_gb, disk_gb} with positive
integer values and override is a partial dictionary of the same shape with
positive integer values, the resolved resource struct merge(defaults, override)
SHALL have each field equal to the override value when present in override and
equal to the default value otherwise, AND every resolved field SHALL be a
positive integer.*

The per-task default table is derived from ``wdl/parameter_template.json``
(the authoritative declaration of per-task resource inputs). We extract the
defaults documented in the parameter_template ``description`` field — the
template itself stores ``default: null`` for every override to signal "use the
task-side default" — and reproduce those numeric defaults here so that
``submit-run.py`` can compute the same resolved struct that the workflow
would compute on its own.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping


# ---------------------------------------------------------------------------
# Per-task default resource table (Design D12, Resource defaults table).
#
# Kept as plain module-level data so tests can reimport without touching the
# filesystem. Numbers mirror the ``description`` defaults in
# ``wdl/parameter_template.json`` and the Design §Resource defaults table.
# Changing a default here must be done in lockstep with the WDL side — the
# static-lint task (15.x) cross-checks both sources.
# ---------------------------------------------------------------------------

TASK_DEFAULTS: dict[str, dict[str, int]] = {
    "pbmm2": {"cpu": 16, "memory_gb": 64, "disk_gb": 500},
    "hifiasm": {"cpu": 48, "memory_gb": 256, "disk_gb": 1500},
    "pav": {"cpu": 32, "memory_gb": 128, "disk_gb": 1000},
    "pav2svs": {"cpu": 2, "memory_gb": 8, "disk_gb": 50},
    "sniffles2": {"cpu": 8, "memory_gb": 32, "disk_gb": 200},
    "sniffles2_merge": {"cpu": 2, "memory_gb": 8, "disk_gb": 100},
    "pbsv_discover": {"cpu": 4, "memory_gb": 16, "disk_gb": 200},
    "pbsv_merge_svsig": {"cpu": 2, "memory_gb": 8, "disk_gb": 100},
    "pbsv_call": {"cpu": 8, "memory_gb": 64, "disk_gb": 200},
    "harmoniser": {"cpu": 8, "memory_gb": 32, "disk_gb": 200},
}

RESOURCE_FIELDS: tuple[str, ...] = ("cpu", "memory_gb", "disk_gb")

# Regex used by :func:`load_defaults_from_template` to pull the numeric
# defaults written in the parameter_template ``description`` strings. The
# template stores ``default: null`` (runtime computes the real default) but
# the description carries the authoritative number.
_DEFAULT_RE = re.compile(r"Default\s+(\d+)", re.IGNORECASE)


def merge_overrides(
    defaults: Mapping[str, int], override: Mapping[str, int]
) -> dict[str, int]:
    """Return a new dict implementing Property 11 merge semantics.

    * Every key in ``defaults`` appears in the output.
    * For each key, the output value is ``override[key]`` when present,
      otherwise ``defaults[key]``.
    * ``override`` may not contain keys absent from ``defaults`` — such keys
      would silently drop resource requests and Property 11 treats them as
      programmer errors, so :class:`KeyError` is raised instead.
    * Every resolved value must be a positive ``int``. Floats, ``bool``, zero,
      and negative values are rejected with :class:`ValueError`.

    The returned dict is a fresh object; callers may mutate it without
    affecting the ``defaults`` / ``override`` inputs.
    """
    merged: dict[str, int] = {}
    for key, value in defaults.items():
        _check_positive_int(key, value, source="default")
        merged[key] = value
    for key, value in override.items():
        if key not in merged:
            raise KeyError(
                f"merge_overrides: override key {key!r} not present in defaults "
                f"(defaults keys: {sorted(merged)!r})"
            )
        _check_positive_int(key, value, source="override")
        merged[key] = value
    return merged


def _check_positive_int(key: str, value: object, *, source: str) -> None:
    """Reject non-positive, non-int, or bool values with a clear message."""
    # ``bool`` is a subclass of ``int``; Property 11 calls for "positive
    # integer" semantics, so booleans are rejected explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"merge_overrides: {source} {key}={value!r} must be a positive int "
            f"(got {type(value).__name__})"
        )
    if value <= 0:
        raise ValueError(
            f"merge_overrides: {source} {key}={value!r} must be > 0"
        )


def resolve_task(
    task_name: str, override: Mapping[str, int] | None = None
) -> dict[str, int]:
    """Resolve ``(defaults, override)`` for a named task into a single dict.

    Convenience wrapper around :func:`merge_overrides` that looks ``task_name``
    up in :data:`TASK_DEFAULTS`. Useful from ``submit-run.py`` when the caller
    has a per-task override dict keyed by task name (parsed from the
    Input_Manifest ``{task}_cpu`` / ``{task}_memory_gb`` / ``{task}_disk_gb``
    fields).
    """
    if task_name not in TASK_DEFAULTS:
        raise KeyError(
            f"resolve_task: unknown task {task_name!r} "
            f"(known: {sorted(TASK_DEFAULTS)!r})"
        )
    return merge_overrides(TASK_DEFAULTS[task_name], override or {})


def load_defaults_from_template(template_path: Path) -> dict[str, dict[str, int]]:
    """Parse ``wdl/parameter_template.json`` and recover per-task defaults.

    The parameter_template stores numeric defaults inside its human-readable
    ``description`` field (``"... Default 16 ..."``); the machine-readable
    ``default`` field is always ``null`` because the real default lives on the
    WDL task itself. This helper scans the descriptions for the first
    ``Default N`` pattern it finds against each ``{task}_{cpu|memory_gb|disk_gb}``
    key and returns the reassembled table.

    Primarily exposed for the property-test / static-lint jobs that want to
    cross-check :data:`TASK_DEFAULTS` against the template.
    """
    data = json.loads(Path(template_path).read_text(encoding="utf-8"))
    table: dict[str, dict[str, int]] = {}
    for key, entry in data.items():
        for field in RESOURCE_FIELDS:
            suffix = f"_{field}"
            if not key.endswith(suffix):
                continue
            task = key[: -len(suffix)]
            description = entry.get("description", "") if isinstance(entry, dict) else ""
            match = _DEFAULT_RE.search(description)
            if match is None:
                continue
            table.setdefault(task, {})[field] = int(match.group(1))
            break
    return table
