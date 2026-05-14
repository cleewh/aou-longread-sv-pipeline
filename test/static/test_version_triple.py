# Feature: aou-longread-sv-pipeline, Property 14: Pipeline version is triple-consistent
"""Version triple consistency check (Task 15.4).

**Validates: Requirement 16.3**

The pipeline version string appears in three places and must be equal
in all of them:

1. ``VERSION`` at the repo root (single line).
2. ``wdl/main.wdl`` ``meta { version: "<X.Y.Z>" }``.
3. ``test/fixtures/run_metadata.json`` ``pipeline.version`` — this
   fixture is added by Task 22 (the real E2E run produces a canonical
   ``run_metadata.json`` that is then snapshotted under ``test/fixtures/``).

At this point in the build Task 22 has not yet produced the fixture,
so the third assertion is skipped with a pointer to Task 22. The first
two assertions always run; once the fixture lands the skip auto-lifts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VERSION_FILE = _REPO_ROOT / "VERSION"
_MAIN_WDL = _REPO_ROOT / "wdl" / "main.wdl"
_FIXTURE = _REPO_ROOT / "test" / "fixtures" / "run_metadata.json"

_META_VERSION_RE = re.compile(
    r"meta\s*\{[^}]*?version\s*:\s*\"([^\"]+)\"",
    re.DOTALL,
)


def _read_version_file() -> str:
    return _VERSION_FILE.read_text(encoding="utf-8").splitlines()[0].strip()


def _read_main_wdl_meta_version() -> str:
    text = _MAIN_WDL.read_text(encoding="utf-8")
    match = _META_VERSION_RE.search(text)
    assert match is not None, (
        f"{_MAIN_WDL.relative_to(_REPO_ROOT)}: no ``meta {{ version: \"...\" }}`` "
        f"found"
    )
    return match.group(1).strip()


@pytest.mark.property_test
def test_version_file_and_main_wdl_meta_version_match():
    """VERSION and main.wdl meta.version must be equal."""
    version_file = _read_version_file()
    main_wdl_version = _read_main_wdl_meta_version()
    assert version_file == main_wdl_version, (
        f"VERSION={version_file!r} != main.wdl meta.version={main_wdl_version!r}"
    )


@pytest.mark.property_test
def test_run_metadata_fixture_pipeline_version_matches():
    """If the Task 22 fixture is present, its pipeline.version must match."""
    if not _FIXTURE.exists():
        pytest.skip(
            "test/fixtures/run_metadata.json not present yet — Task 22 "
            "produces this fixture from the first real E2E run"
        )
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    pipeline = payload.get("pipeline", {})
    fixture_version = pipeline.get("version")
    assert isinstance(fixture_version, str) and fixture_version, (
        f"{_FIXTURE.relative_to(_REPO_ROOT)}: pipeline.version missing or empty"
    )
    version_file = _read_version_file()
    assert fixture_version == version_file, (
        f"fixture pipeline.version={fixture_version!r} != "
        f"VERSION={version_file!r}"
    )
