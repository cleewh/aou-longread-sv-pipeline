# Feature: aou-longread-sv-pipeline, Property 6: Every ECR image reference is in ap-southeast-1 and digest-pinned
# Feature: aou-longread-sv-pipeline, Property 10: Every WDL task declares cpu, memory, disks
"""Static WDL lint checks (Task 15.1).

**Validates: Requirements 8.1, 8.2, 10.3, 11.1, 17.7** (ECR portion)

Two properties are enforced here by regex-based WDL parsing. Regex is
used in preference to :mod:`WDL` / miniwdl because the task only needs
to look at a handful of fields (``docker``, ``cpu``, ``memory``,
``disks``) in a file set that we control end-to-end. This keeps the
test's cost low and removes a hard dependency on miniwdl being
importable at collection time.

Property 6: every ``runtime.docker`` reference in every task under
``wdl/tasks/*.wdl`` (and ``wdl/main.wdl`` if a task is present there,
though the workflow is currently task-free at the top level) is hosted
in the ``ap-southeast-1`` ECR registry AND is digest-pinned with a
``@sha256:<64-hex>`` reference — never a floating ``:tag`` reference.

Property 10: every task declares non-empty ``cpu``, ``memory``, and
``disks`` runtime keys.

The test also asserts the absence of Terra-only ``preemptible`` /
``bootDiskSizeGb`` / ``zones`` runtime keys (HealthOmics rejects these)
and the absence of ``glob(..."s3://..."...)`` calls (HealthOmics
localises inputs per-task and does not support direct S3 globbing).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Regex and constants
# ---------------------------------------------------------------------------

# Digest-pinned ECR URI in ap-southeast-1. The ``\d{12}`` at the start
# matches the AWS account id; the repo slug after ``aou-sv/`` is at
# minimum one path segment. No ``:tag`` is permitted — only ``@sha256:``.
_ECR_URI_RE = re.compile(
    r"^\d{12}\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/[^:@]+@sha256:[0-9a-f]{64}$"
)

# Task header — ``task <Name> {`` anchored at line start, with
# whitespace tolerance.
_TASK_HEADER_RE = re.compile(r"^\s*task\s+(\w+)\s*\{", re.MULTILINE)

# Runtime block header — matches ``runtime {`` and records the offset of
# the opening brace so the caller can brace-count the body. A naive
# non-greedy match breaks because runtime bodies typically contain WDL
# ``~{memory_gb}`` interpolations whose inner ``}`` trips the regex.
_RUNTIME_HEADER_RE = re.compile(r"runtime\s*\{")


def _extract_brace_body(text: str, open_brace_offset: int) -> str:
    """Return the body between ``text[open_brace_offset]`` (a ``{``) and
    its matching ``}``. Works in the presence of nested braces such as
    WDL ``~{expr}`` interpolations.
    """
    assert text[open_brace_offset] == "{"
    depth = 1
    i = open_brace_offset + 1
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[open_brace_offset + 1 : i - 1] if depth == 0 else text[open_brace_offset + 1 :]


def _runtime_body(task_body: str) -> str | None:
    """Return the content of the first ``runtime { ... }`` block in ``task_body``."""
    header = _RUNTIME_HEADER_RE.search(task_body)
    if header is None:
        return None
    # The opening brace is the last character of the matched header.
    return _extract_brace_body(task_body, header.end() - 1)

_DOCKER_RE = re.compile(r'docker\s*:\s*"([^"]+)"')
_CPU_RE    = re.compile(r"cpu\s*:\s*(\S+)")
_MEMORY_RE = re.compile(r'memory\s*:\s*"([^"]+)"')
_DISKS_RE  = re.compile(r'disks\s*:\s*"([^"]+)"')

# Terra-only runtime keys (see Design D2 / pav.wdl comment).
_TERRA_KEYS = ("preemptible:", "bootDiskSizeGb", "zones:")

# glob(...) over an s3:// URI. The upstream Terra / Cromwell runtime
# supported this; HealthOmics does not. Matches both ``glob("s3://...")``
# and ``glob('s3://...')``.
_GLOB_S3_RE = re.compile(r'glob\s*\(\s*["\']s3://')

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WDL_DIR   = _REPO_ROOT / "wdl"


def _wdl_files() -> list[Path]:
    """Return every WDL file the lint rules apply to.

    This is the tasks under ``wdl/tasks/`` plus ``wdl/main.wdl``.
    ``wdl/structs.wdl`` is intentionally excluded because structs do
    not carry ``runtime`` blocks.
    """
    files = sorted((_WDL_DIR / "tasks").glob("*.wdl"))
    main = _WDL_DIR / "main.wdl"
    if main.exists():
        files.append(main)
    return files


def _iter_tasks(wdl_text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(task_name, task_body)`` pairs for every task in the file.

    A task body is the text from the opening ``{`` through the matching
    closing ``}``. Brace matching is done by counting; WDL task bodies
    contain nested braces in command blocks (``<<< ... >>>`` delimiters
    are brace-free, but inner scalar expressions use ``~{...}``) so a
    simple regex would miscount.
    """
    for match in _TASK_HEADER_RE.finditer(wdl_text):
        task_name = match.group(1)
        body_start = match.end()
        # Brace-count from body_start - 1 (the ``{`` itself) to find the
        # matching close. WDL command blocks use ``<<<`` / ``>>>`` which
        # are brace-free; ``~{...}`` interpolations close on the same line
        # they open. Net: a naive counter works.
        depth = 1
        i = body_start
        while i < len(wdl_text) and depth > 0:
            ch = wdl_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        body = wdl_text[body_start : i - 1] if depth == 0 else wdl_text[body_start:]
        yield task_name, body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
def test_every_task_docker_uri_is_ecr_apse1_digest_pinned():
    """Property 6: every runtime.docker is an ap-southeast-1 ECR digest URI."""
    violations: list[str] = []
    task_count = 0
    for path in _wdl_files():
        text = path.read_text(encoding="utf-8")
        for task_name, body in _iter_tasks(text):
            task_count += 1
            runtime_body = _runtime_body(body)
            if runtime_body is None:
                violations.append(
                    f"{path.relative_to(_REPO_ROOT)}::{task_name}: no runtime block"
                )
                continue
            docker_match = _DOCKER_RE.search(runtime_body)
            if docker_match is None:
                violations.append(
                    f"{path.relative_to(_REPO_ROOT)}::{task_name}: no docker key"
                )
                continue
            uri = docker_match.group(1)
            if not _ECR_URI_RE.match(uri):
                violations.append(
                    f"{path.relative_to(_REPO_ROOT)}::{task_name}: "
                    f"docker URI {uri!r} is not an ap-southeast-1 ECR digest reference"
                )
    assert task_count > 0, "no WDL tasks found — test would pass vacuously"
    assert not violations, "Property 6 violations:\n" + "\n".join(violations)


@pytest.mark.property_test
def test_every_task_declares_cpu_memory_disks():
    """Property 10: every task declares non-empty cpu, memory, disks."""
    violations: list[str] = []
    task_count = 0
    for path in _wdl_files():
        text = path.read_text(encoding="utf-8")
        for task_name, body in _iter_tasks(text):
            task_count += 1
            runtime_body = _runtime_body(body)
            if runtime_body is None:
                violations.append(
                    f"{path.relative_to(_REPO_ROOT)}::{task_name}: no runtime block"
                )
                continue
            cpu_match    = _CPU_RE.search(runtime_body)
            memory_match = _MEMORY_RE.search(runtime_body)
            disks_match  = _DISKS_RE.search(runtime_body)
            label = f"{path.relative_to(_REPO_ROOT)}::{task_name}"
            if cpu_match is None or not cpu_match.group(1).strip():
                violations.append(f"{label}: missing or empty cpu")
            if memory_match is None or not memory_match.group(1).strip():
                violations.append(f"{label}: missing or empty memory")
            if disks_match is None or not disks_match.group(1).strip():
                violations.append(f"{label}: missing or empty disks")
    assert task_count > 0, "no WDL tasks found — test would pass vacuously"
    assert not violations, "Property 10 violations:\n" + "\n".join(violations)


@pytest.mark.property_test
def test_no_terra_only_runtime_keys():
    """HealthOmics rejects Terra-only keys; ensure none slipped in."""
    violations: list[str] = []
    for path in _wdl_files():
        text = path.read_text(encoding="utf-8")
        for task_name, body in _iter_tasks(text):
            runtime_body = _runtime_body(body)
            if runtime_body is None:
                continue
            for key in _TERRA_KEYS:
                if key in runtime_body:
                    violations.append(
                        f"{path.relative_to(_REPO_ROOT)}::{task_name}: "
                        f"Terra-only runtime key {key!r} present"
                    )
    assert not violations, "Terra-only key violations:\n" + "\n".join(violations)


@pytest.mark.property_test
def test_no_glob_over_s3_uris():
    """HealthOmics does not support WDL ``glob(...)`` over ``s3://`` URIs."""
    violations: list[str] = []
    for path in _wdl_files():
        text = path.read_text(encoding="utf-8")
        for match in _GLOB_S3_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            violations.append(
                f"{path.relative_to(_REPO_ROOT)}:{line_no}: "
                f"glob(...) over s3:// URI"
            )
    assert not violations, "glob-over-s3 violations:\n" + "\n".join(violations)
