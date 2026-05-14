# Feature: aou-longread-sv-pipeline, Property (SOURCES.md schema): all required H2 sections present
"""SOURCES.md schema check (Task 15.6).

**Validates: Requirements 1.2, 17.4, 17.14**

``SOURCES.md`` is the human-readable sibling of
``containers/manifest.yaml`` and the log of every upstream artefact,
measurement, and deviation the pipeline depends on. Downstream tests,
the deploy flow, and the cost / HWM static checks all depend on a
stable section layout. This test enforces that every required H2
section exists; additional sections are tolerated.

The "non-empty" check only requires at least one non-whitespace,
non-HTML-comment line of body content. This keeps the check cheap to
evaluate and permits scaffold-time sections whose real content lands
in later tasks — as long as a placeholder row or prose paragraph is
present, the section passes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SOURCES_MD = _REPO_ROOT / "SOURCES.md"

_REQUIRED_SECTIONS = (
    "Upstream commits",
    "Disabled non-essential options",
    "Measured high-water marks",
    "Graviton matrix",
    "Image digests",
    "Adaptation notes",
    "Pricing source",
    "Dockerfile lint",
)

_SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")


def _section_bodies(sources_md: str) -> dict[str, str]:
    """Return ``{section_title: body}`` for every ``## <title>`` header."""
    out: dict[str, str] = {}
    matches = list(_SECTION_HEADER_RE.finditer(sources_md))
    for i, match in enumerate(matches):
        title = match.group(1).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(sources_md)
        out[title] = sources_md[body_start:body_end]
    return out


def _non_empty(body: str) -> bool:
    """True when ``body`` contains at least one non-comment, non-blank line."""
    stripped = _HTML_COMMENT_RE.sub("", body)
    for line in stripped.splitlines():
        if line.strip():
            return True
    return False


@pytest.mark.property_test
def test_sources_md_has_all_required_sections():
    text = _SOURCES_MD.read_text(encoding="utf-8")
    bodies = _section_bodies(text)
    missing = [title for title in _REQUIRED_SECTIONS if title not in bodies]
    assert not missing, (
        f"SOURCES.md missing required H2 sections: {missing!r}. "
        f"Present sections: {sorted(bodies)!r}"
    )


@pytest.mark.property_test
def test_sources_md_required_sections_are_non_empty():
    text = _SOURCES_MD.read_text(encoding="utf-8")
    bodies = _section_bodies(text)
    empty = [
        title for title in _REQUIRED_SECTIONS
        if title in bodies and not _non_empty(bodies[title])
    ]
    assert not empty, (
        f"SOURCES.md required sections are empty (only whitespace or "
        f"HTML comments): {empty!r}"
    )
