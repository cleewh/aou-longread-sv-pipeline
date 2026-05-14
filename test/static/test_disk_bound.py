# Feature: aou-longread-sv-pipeline, Property 16: Task disk_gb defaults bounded by measured high-water + 25%
"""Disk-bound default check (Task 15.5).

**Validates: Requirement 17.4**

For every task that has a measured high-water-mark entry under
``SOURCES.md`` ``## Measured high-water marks``, the task's default
``disk_gb`` in the WDL must satisfy::

    default_disk_gb <= ceil(hwm_gb * 1.25)

i.e. no more than 25 % headroom over the observed peak. This keeps us
from over-provisioning storage and silently inflating cost.

At this stage of the repo the HWM table is empty — Task 22's HG002
chr20 E2E run populates it. The test therefore skips at runtime when
the table body contains only the HTML-comment placeholder, and
auto-activates once Task 22 fills it in.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SOURCES_MD = _REPO_ROOT / "SOURCES.md"
_WDL_DIR = _REPO_ROOT / "wdl"

_SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_TASK_HEADER_RE = re.compile(r"^\s*task\s+(\w+)\s*\{", re.MULTILINE)
_DISK_DEFAULT_RE = re.compile(
    r"Int\s+disk_gb\s*=\s*(\d+)",
)


def _section_body(sources_md: str, section_title: str) -> str:
    """Return the body of the ``## <section_title>`` section.

    The body is the text between this section's header and the next
    ``##`` header (or EOF). Leading/trailing whitespace is stripped.
    """
    matches = list(_SECTION_HEADER_RE.finditer(sources_md))
    for i, match in enumerate(matches):
        if match.group(1).strip() == section_title:
            body_start = match.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(sources_md)
            return sources_md[body_start:body_end].strip()
    raise AssertionError(
        f"SOURCES.md: required section '## {section_title}' not found"
    )


def _parse_hwm_table(body: str) -> dict[str, int]:
    """Parse the HWM markdown table body into ``{task_name: hwm_gb}``.

    Expected row shape (post-Task 22)::

        | Task | CPU hwm | RSS GB hwm | Disk GB hwm | Source |
        |------|---------|------------|-------------|--------|
        | PAV_Run | 12 | 90 | 450 | HG002 chr20 run <id> |

    Non-data rows (the header, the separator, or anything that is not
    a pipe-delimited row with at least a numeric Disk GB column) are
    skipped. Returns the empty dict when the section body contains only
    HTML comments / whitespace.
    """
    out: dict[str, int] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("<!--") or line.startswith("-->"):
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        # Skip the header row and the separator row.
        if cells[0].lower() == "task" or set(cells[0]) <= {"-", ":"}:
            continue
        task_name = cells[0]
        disk_cell = cells[3]
        try:
            disk_gb = int(re.sub(r"[^\d]", "", disk_cell))
        except ValueError:
            continue
        if not disk_gb:
            continue
        out[task_name] = disk_gb
    return out


def _task_disk_defaults() -> dict[str, int]:
    """Return ``{task_name: default_disk_gb}`` from every WDL task."""
    out: dict[str, int] = {}
    wdl_files = sorted((_WDL_DIR / "tasks").glob("*.wdl"))
    main = _WDL_DIR / "main.wdl"
    if main.exists():
        wdl_files.append(main)
    for path in wdl_files:
        text = path.read_text(encoding="utf-8")
        # Iterate tasks via header regex and, for each task body, take
        # the first ``Int disk_gb = <N>`` default.
        headers = list(_TASK_HEADER_RE.finditer(text))
        for i, header in enumerate(headers):
            task_name = header.group(1)
            body_start = header.end()
            body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            body = text[body_start:body_end]
            match = _DISK_DEFAULT_RE.search(body)
            if match is not None:
                out[task_name] = int(match.group(1))
    return out


@pytest.mark.property_test
def test_disk_defaults_within_25pct_of_measured_hwm():
    """Property 16: default_disk_gb <= ceil(hwm_gb * 1.25)."""
    body = _section_body(_SOURCES_MD.read_text(encoding="utf-8"),
                         "Measured high-water marks")
    hwm_by_task = _parse_hwm_table(body)
    if not hwm_by_task:
        pytest.skip(
            "SOURCES.md ## Measured high-water marks section empty — "
            "populated by Task 22 (HG002 chr20 E2E run). Test will "
            "auto-activate once the table lands."
        )

    defaults_by_task = _task_disk_defaults()
    violations: list[str] = []
    for task_name, hwm_gb in hwm_by_task.items():
        default_gb = defaults_by_task.get(task_name)
        if default_gb is None:
            violations.append(
                f"{task_name}: HWM row present in SOURCES.md but no WDL "
                f"default disk_gb found"
            )
            continue
        bound = math.ceil(hwm_gb * 1.25)
        if default_gb > bound:
            violations.append(
                f"{task_name}: default disk_gb={default_gb} > "
                f"ceil(hwm_gb={hwm_gb} * 1.25)={bound}"
            )
    assert not violations, "Property 16 violations:\n" + "\n".join(violations)
