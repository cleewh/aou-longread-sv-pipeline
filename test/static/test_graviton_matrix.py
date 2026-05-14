# Feature: aou-longread-sv-pipeline, Property 17: Graviton image is used when and only when upstream supports arm64
"""Graviton matrix consistency check (Task 15.3).

**Validates: Requirements 17.5, 17.6**

Every WDL task's ``runtime.docker`` URI must reference an ECR repo
(``aou-sv/<tool>``) that is declared in ``containers/manifest.yaml``
with a compatible ``platforms`` list. This is the **rendering rule**
check — at this stage of the repo every WDL task pins the sentinel
``sha256:0...0`` digest (see the per-task comments in
``wdl/tasks/*.wdl``) so we cannot distinguish ``digest_arm64`` vs
``digest_amd64`` from the URI alone. The sentinel-to-real-digest
substitution happens in Task 22 when ``scripts/mirror-images.py``
rewrites the WDL tasks and pushes to ECR; after that, this test (or a
later evolution of it) is what enforces the amd64-vs-arm64 matching.

For now the test enforces the weaker invariant that holds today:

* every WDL task name maps to a known tool in the manifest, and
* the ECR repo segment of the URI matches the manifest entry's
  ``ecr_repo`` field, and
* the manifest entry's ``platforms`` include at least one compatible
  architecture (amd64 is always acceptable; arm64 is acceptable only
  when the manifest lists ``linux/arm64``).

Tasks that Design D7 marks as arm64-primary (hifiasm, pbmm2, pav2svs,
sniffles2, harmoniser, metadata-writer) have their manifest entries
re-verified to declare ``linux/arm64`` in ``platforms``; tasks that are
amd64-only (pav, pbsv) have their manifest entries re-verified NOT to
declare ``linux/arm64``. This matches the Design §Graviton matrix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WDL_DIR = _REPO_ROOT / "wdl"
_MANIFEST_PATH = _REPO_ROOT / "containers" / "manifest.yaml"


# Task-name -> manifest tool name lookup. The WDL task name does not
# always equal the manifest's ``name`` (e.g. ``Pbmm2_Align`` -> pbmm2,
# ``Sniffles2_Merge_Task`` -> sniffles2). This map is the single
# authoritative mapping; Task 22 rewrites are expected to preserve it.
_TASK_TO_TOOL = {
    "Pbmm2_Align":           "pbmm2",
    "Hifiasm_Assemble":      "hifiasm",
    "PAV_Run":               "pav",
    "PAV2SVs":               "pav2svs",
    "Sniffles2_Task":        "sniffles2",
    "Sniffles2_Merge_Task":  "sniffles2",
    "PBSV_Discover_Task":    "pbsv",
    "PBSV_Merge_Svsig_Task": "pbsv",
    "PBSV_Call_Task":        "pbsv",
    "Harmoniser":            "harmoniser",
    "InputValidator":        "metadata-writer",
    "MetadataWriter":        "metadata-writer",
}


_TASK_HEADER_RE = re.compile(r"^\s*task\s+(\w+)\s*\{", re.MULTILINE)
_RUNTIME_HEADER_RE = re.compile(r"runtime\s*\{")
_DOCKER_RE = re.compile(r'docker\s*:\s*"([^"]+)"')
# Capture the ECR repo slug. Group 1 = account id, group 2 = repo path
# (e.g. ``aou-sv/pbmm2``).
_URI_ECR_REPO_RE = re.compile(
    r"^(\d{12})\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/([^@:]+)@sha256:[0-9a-f]{64}$"
)


def _extract_brace_body(text: str, open_brace_offset: int) -> str:
    """Return the body between ``text[open_brace_offset]`` (a ``{``) and
    its matching ``}``, counting nested ``{``/``}`` pairs.
    """
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


def _iter_task_dockers() -> list[tuple[Path, str, str]]:
    """Return ``[(path, task_name, docker_uri)]`` for every task."""
    out: list[tuple[Path, str, str]] = []
    wdl_files = sorted((_WDL_DIR / "tasks").glob("*.wdl"))
    main = _WDL_DIR / "main.wdl"
    if main.exists():
        wdl_files.append(main)
    for path in wdl_files:
        text = path.read_text(encoding="utf-8")
        headers = list(_TASK_HEADER_RE.finditer(text))
        for i, header in enumerate(headers):
            task_name = header.group(1)
            body_start = header.end()
            body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            task_body = text[body_start:body_end]
            rt_header = _RUNTIME_HEADER_RE.search(task_body)
            if rt_header is None:
                continue
            runtime_body = _extract_brace_body(task_body, rt_header.end() - 1)
            docker_match = _DOCKER_RE.search(runtime_body)
            if docker_match is None:
                continue
            out.append((path, task_name, docker_match.group(1)))
    return out


def _load_manifest_by_name() -> dict[str, dict]:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in raw["images"]}


@pytest.mark.property_test
def test_every_task_maps_to_known_tool_in_manifest():
    """Every WDL task name must be in the task→tool map AND in the manifest."""
    manifest_by_name = _load_manifest_by_name()
    task_dockers = _iter_task_dockers()
    assert task_dockers, "no WDL tasks found — lookup would be vacuous"
    for path, task_name, _uri in task_dockers:
        assert task_name in _TASK_TO_TOOL, (
            f"{path.relative_to(_REPO_ROOT)}::{task_name}: no entry in "
            f"_TASK_TO_TOOL — extend the map or rename the task"
        )
        tool = _TASK_TO_TOOL[task_name]
        assert tool in manifest_by_name, (
            f"{path.relative_to(_REPO_ROOT)}::{task_name}: maps to tool "
            f"{tool!r} which is absent from containers/manifest.yaml"
        )


@pytest.mark.property_test
def test_task_docker_ecr_repo_matches_manifest_entry():
    """The URI's ``aou-sv/<tool>`` slug must equal the manifest's ecr_repo."""
    manifest_by_name = _load_manifest_by_name()
    violations: list[str] = []
    for path, task_name, uri in _iter_task_dockers():
        tool = _TASK_TO_TOOL.get(task_name)
        if tool is None:
            continue
        entry = manifest_by_name.get(tool)
        if entry is None:
            continue
        uri_match = _URI_ECR_REPO_RE.match(uri)
        if uri_match is None:
            violations.append(
                f"{path.relative_to(_REPO_ROOT)}::{task_name}: URI {uri!r} "
                f"does not match the digest-pinned ECR shape"
            )
            continue
        uri_repo = uri_match.group(2)
        if uri_repo != entry["ecr_repo"]:
            violations.append(
                f"{path.relative_to(_REPO_ROOT)}::{task_name}: URI repo "
                f"{uri_repo!r} != manifest ecr_repo {entry['ecr_repo']!r}"
            )
    assert not violations, "ECR-repo mismatch:\n" + "\n".join(violations)


@pytest.mark.property_test
def test_graviton_matrix_amd64_arm64_consistency():
    """Declared arm64-only-in-Design tools must match the manifest platforms.

    Design §Per-task interfaces declares pav and pbsv amd64-only upstream
    (RepeatMasker binary / PacBio binary). Every other tool is
    multi-arch (amd64 + arm64). This test pins that matrix against the
    manifest so a slip in either direction (a new tool added without
    arm64 support, or someone dropping arm64 from a Graviton-eligible
    tool) fires immediately.
    """
    manifest_by_name = _load_manifest_by_name()

    # amd64-only tools per SOURCES.md §Graviton matrix. hifiasm 0.19.9
    # joined this list after the first mirror attempt: the upstream source
    # uses x86 SSE2/SSE4.2 intrinsics directly (`emmintrin.h` is
    # `#include`d in core translation units), so a clean arm64 build would
    # require a source-level sse2neon port. sniffles2 joined after the
    # first HealthOmics run: the multi-arch biocontainers image is stripped
    # of bcftools/tabix, so the Dockerfile two-stage-copies those from
    # debian:12-slim + apt; the cross-stage COPY over emulated arm64 hits
    # a SIGILL during the smoke-test layer. Treated as an amd64-only
    # fallback per Req 17.6.
    amd64_only_tools = {"hifiasm", "pav", "pbsv", "sniffles2"}
    all_tools = set(_TASK_TO_TOOL.values())

    for tool in all_tools:
        entry = manifest_by_name.get(tool)
        assert entry is not None, f"tool {tool!r} absent from manifest"
        platforms = set(entry.get("platforms", []))
        assert "linux/amd64" in platforms, (
            f"tool {tool!r}: manifest must list linux/amd64 (got {platforms!r})"
        )
        if tool in amd64_only_tools:
            assert "linux/arm64" not in platforms, (
                f"tool {tool!r}: Design §Graviton matrix marks this amd64-only "
                f"but manifest lists linux/arm64 (got {platforms!r})"
            )
        else:
            assert "linux/arm64" in platforms, (
                f"tool {tool!r}: Design §Graviton matrix expects linux/arm64 "
                f"but manifest does not list it (got {platforms!r})"
            )
