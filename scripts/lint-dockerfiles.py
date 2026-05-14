#!/usr/bin/env python3
"""Static-only Dockerfile / manifest lint for containers/*/Dockerfile.

Task 4.6 would ordinarily invoke ``scripts/mirror-images.py --dry-run``
against every entry in ``containers/manifest.yaml`` to prove each
Dockerfile builds end-to-end. That requires a working local Docker
daemon with multi-arch buildx. On the current host the Docker daemon
is gated behind an enterprise sign-in requirement, so the full dry-run
build is deferred to Task 22 (the live E2E run).

This script performs the subset of Task 4.6 that does not require the
Docker daemon: it parses every Dockerfile syntactically and asserts the
cheap properties an ``--dry-run`` build would also have caught:

1. For every ``containers/<name>/Dockerfile`` under the repo:
   - at least one ``FROM`` directive;
   - if ``ARG TARGETARCH`` is declared, every stage name substituted by
     ``${TARGETARCH}`` (i.e. ``amd64-stage`` and ``arm64-stage``) is
     actually defined as an ``AS <name>`` on an earlier ``FROM`` line;
   - every directive token is a recognised Dockerfile keyword
     (rejecting typos like ``RUNN``, ``COP``, ``ENTTRYPOINT``);
   - every ``COPY`` / ``ADD`` source path exists in the expected build
     context. When a sibling marker file named ``BUILD_CONTEXT_REPO_ROOT``
     is present next to the Dockerfile, the build context is the
     repository root (so ``COPY pricing/...`` and
     ``COPY containers/<name>/src/...`` are both valid when the named
     paths exist). Otherwise the build context is the per-tool folder,
     so ``COPY src/`` is valid iff ``containers/<name>/src/`` exists.
     ``--from=<stage>`` copies and ``ADD <url>`` fetches are skipped.
2. For ``containers/manifest.yaml`` round-trip: every image entry's
   ``ecr_repo`` of the form ``aou-sv/<tool>`` must correspond to a
   directory under ``containers/<tool>/`` that either contains a
   ``Dockerfile`` OR has an ``upstream`` that points at a Docker
   registry (for pure-mirror images that need no local Dockerfile).
3. Results are logged to stdout. Exits 0 iff every check passes.

This file is a CLI tool, not a pytest module: it is intentionally not
discovered by pytest's collection and does not import anything from the
test harness. Run it manually with::

    python3 aou-longread-sv-pipeline/scripts/lint-dockerfiles.py

Requirements: 8.1, 8.3, 8.4 (partial — full push deferred to Task 22),
17.5, 17.6.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Dockerfile grammar
# ---------------------------------------------------------------------------

# https://docs.docker.com/reference/dockerfile/ — every official directive,
# plus the deprecated MAINTAINER which some older upstream Dockerfiles still
# emit. Anything outside this set is almost certainly a typo.
RECOGNISED_DIRECTIVES = frozenset(
    {
        "FROM",
        "MAINTAINER",
        "RUN",
        "CMD",
        "LABEL",
        "EXPOSE",
        "ENV",
        "ADD",
        "COPY",
        "ENTRYPOINT",
        "VOLUME",
        "USER",
        "WORKDIR",
        "ARG",
        "ONBUILD",
        "STOPSIGNAL",
        "HEALTHCHECK",
        "SHELL",
    }
)

# Substitutions the image matrix expects for ``${TARGETARCH}`` (see
# containers/manifest.yaml). Kept narrow on purpose: this lint does not
# try to emulate the full Docker build-arg expansion engine.
TARGETARCH_SUBSTITUTIONS = ("amd64", "arm64")

# ECR repository prefix required by Requirement 8.1 — every entry in
# ``containers/manifest.yaml`` routes to ``aou-sv/<tool>``.
ECR_REPO_PREFIX = "aou-sv/"


# ---------------------------------------------------------------------------
# Logical-instruction parser
# ---------------------------------------------------------------------------


def iter_logical_instructions(text: str) -> Iterable[Tuple[int, str, str]]:
    """Yield ``(lineno, directive, args)`` for each logical instruction.

    Handles:

    * whole-line ``#`` comments (skipped; Docker does not support inline
      ``#`` comments inside instructions — a ``#`` appearing inside a
      ``RUN`` command is shell syntax, not a Dockerfile comment);
    * blank lines (skipped);
    * line continuations: a trailing backslash joins the next line into
      the same logical instruction, matching Docker's own parser.

    ``lineno`` is 1-indexed and points at the line where the instruction
    *starts* so callers can produce useful error messages.
    """
    physical_lines = text.splitlines()
    i = 0
    while i < len(physical_lines):
        raw = physical_lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        start_line = i + 1
        # Accumulate continuations.
        logical_parts: List[str] = []
        current = raw
        while current.rstrip().endswith("\\"):
            logical_parts.append(current.rstrip()[:-1])
            i += 1
            if i >= len(physical_lines):
                break
            current = physical_lines[i]
        logical_parts.append(current)
        logical = " ".join(part.strip() for part in logical_parts).strip()
        i += 1

        # First token is the directive; split on first whitespace only so
        # the rest is the argument string verbatim (preserving JSON-array
        # CMD / ENTRYPOINT, quoted ENV values, etc.).
        pieces = logical.split(None, 1)
        directive = pieces[0]
        args = pieces[1] if len(pieces) > 1 else ""
        yield start_line, directive, args


# ---------------------------------------------------------------------------
# Per-directive extractors
# ---------------------------------------------------------------------------


def parse_from(args: str) -> Tuple[str, Optional[str]]:
    """Return ``(image_ref, stage_alias_or_None)`` for a ``FROM`` arg string.

    Handles ``FROM image AS name`` and plain ``FROM image``. The ``AS``
    keyword is case-insensitive per Dockerfile spec.
    """
    # Strip ``--platform=...`` optional flag if present.
    tokens = args.split()
    filtered = [t for t in tokens if not t.startswith("--")]
    if not filtered:
        return "", None
    if len(filtered) >= 3 and filtered[-2].upper() == "AS":
        return " ".join(filtered[:-2]), filtered[-1]
    return " ".join(filtered), None


def parse_copy_or_add_sources(args: str) -> Tuple[List[str], Optional[str]]:
    """Return ``(source_paths, dest_or_None)`` for ``COPY`` / ``ADD`` args.

    Skips:
      * ``--chown=``, ``--chmod=``, ``--from=`` and other flag tokens;
      * signals ``--from=<stage>`` to the caller via the returned tuple
        shape: when ``--from=`` is present we return ``([], None)`` so the
        caller knows to skip the build-context-existence check (sources
        come from a prior build stage, not the filesystem).

    For ``ADD`` sources that are URLs (``http://`` / ``https://``), the
    caller skips them because Docker fetches them over the network rather
    than resolving against the build context.
    """
    tokens = args.split()
    # JSON array form: ``COPY ["src", "dest"]``. We don't bother parsing
    # the JSON here; the lint does not use JSON-array COPY forms in this
    # repo. Return nothing so the existence check is skipped.
    if tokens and tokens[0].startswith("["):
        return [], None

    flag_tokens = [t for t in tokens if t.startswith("--")]
    if any(t.startswith("--from=") for t in flag_tokens):
        return [], None  # sources come from another build stage

    positional = [t for t in tokens if not t.startswith("--")]
    if len(positional) < 2:
        return [], None
    sources = positional[:-1]
    dest = positional[-1]
    return sources, dest


# ---------------------------------------------------------------------------
# Dockerfile-level checks
# ---------------------------------------------------------------------------


def lint_one_dockerfile(dockerfile: Path, workdir: Path) -> List[str]:
    """Return a list of human-readable problems for ``dockerfile``.

    Empty list ⇒ the Dockerfile passes every check.
    """
    problems: List[str] = []
    text = dockerfile.read_text(encoding="utf-8")
    instructions = list(iter_logical_instructions(text))

    if not instructions:
        problems.append("file is empty or contains only comments")
        return problems

    # --- Unknown directive scan. --------------------------------------------
    for lineno, directive, _args in instructions:
        if directive.upper() not in RECOGNISED_DIRECTIVES:
            problems.append(
                f"line {lineno}: unrecognised directive {directive!r} "
                f"(typo? allowed: {sorted(RECOGNISED_DIRECTIVES)})"
            )

    # --- At-least-one-FROM + stage collection + TARGETARCH wiring. ---------
    from_lines: List[Tuple[int, str, Optional[str]]] = []  # (lineno, ref, alias)
    declares_targetarch = False
    for lineno, directive, args in instructions:
        up = directive.upper()
        if up == "FROM":
            image_ref, alias = parse_from(args)
            from_lines.append((lineno, image_ref, alias))
        elif up == "ARG":
            # ``ARG TARGETARCH`` or ``ARG TARGETARCH=default``.
            arg_name = args.split("=", 1)[0].strip()
            if arg_name == "TARGETARCH":
                declares_targetarch = True

    if not from_lines:
        problems.append("no FROM directive found")
        return problems

    stage_aliases = {alias for _, _, alias in from_lines if alias}
    targetarch_consumers = [
        (lineno, ref)
        for lineno, ref, _ in from_lines
        if "${TARGETARCH}" in ref or "$TARGETARCH" in ref
    ]
    if declares_targetarch and targetarch_consumers:
        for substitution in TARGETARCH_SUBSTITUTIONS:
            expected_stage = f"{substitution}-stage"
            if expected_stage not in stage_aliases:
                problems.append(
                    f"ARG TARGETARCH is declared and a later FROM uses "
                    f"${{TARGETARCH}}-stage, but stage alias "
                    f"{expected_stage!r} is not defined "
                    f"(found aliases: {sorted(stage_aliases)})"
                )

    # --- COPY / ADD source existence against the build context. ------------
    tool_dir = dockerfile.parent
    if (tool_dir / "BUILD_CONTEXT_REPO_ROOT").exists():
        build_context = workdir
        context_label = f"repo root ({workdir})"
    else:
        build_context = tool_dir
        context_label = f"{tool_dir}"

    for lineno, directive, args in instructions:
        up = directive.upper()
        if up not in ("COPY", "ADD"):
            continue
        sources, _dest = parse_copy_or_add_sources(args)
        if not sources:
            continue
        for src in sources:
            if up == "ADD" and (src.startswith("http://") or src.startswith("https://")):
                continue  # URL fetch, not a build-context path
            # Strip trailing slash to let ``Path.exists()`` match a directory.
            candidate = build_context / src.rstrip("/")
            if not candidate.exists():
                problems.append(
                    f"line {lineno}: {up} source {src!r} not found under "
                    f"build context {context_label}"
                )

    return problems


# ---------------------------------------------------------------------------
# Manifest-level cross-check
# ---------------------------------------------------------------------------


def _looks_like_docker_registry_ref(upstream: str) -> bool:
    """Heuristic: ``upstream`` points at a Docker registry image iff it is a
    non-empty string that is not obviously a local base image being
    extended by a Dockerfile.

    We accept any non-empty ``upstream``; the real registry-mirror path is
    exercised by ``scripts/mirror-images.py`` in Task 22. Here we just
    want to reject empty / missing fields.
    """
    return bool(upstream.strip())


def lint_manifest(manifest_path: Path, workdir: Path) -> List[str]:
    """Cross-check every manifest entry has either a local Dockerfile
    OR a non-empty upstream registry reference.

    Additional guarantees:
      * ``ecr_repo`` starts with ``aou-sv/`` (Requirement 8.1);
      * the ``<tool>`` suffix of ``ecr_repo`` matches a directory under
        ``containers/<tool>/``.
    """
    problems: List[str] = []
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "images" not in data:
        return [f"{manifest_path}: missing top-level 'images' key"]

    for entry in data["images"]:
        name = entry.get("name", "<anon>")
        ecr_repo = entry.get("ecr_repo", "")
        upstream = entry.get("upstream", "")
        if not ecr_repo.startswith(ECR_REPO_PREFIX):
            problems.append(
                f"manifest entry {name!r}: ecr_repo {ecr_repo!r} does not "
                f"start with {ECR_REPO_PREFIX!r}"
            )
            continue
        tool = ecr_repo[len(ECR_REPO_PREFIX):]
        tool_dir = workdir / "containers" / tool
        if not tool_dir.is_dir():
            problems.append(
                f"manifest entry {name!r}: containers/{tool}/ directory "
                f"does not exist"
            )
            continue
        has_dockerfile = (tool_dir / "Dockerfile").exists()
        has_upstream = _looks_like_docker_registry_ref(upstream)
        if not (has_dockerfile or has_upstream):
            problems.append(
                f"manifest entry {name!r}: neither containers/{tool}/Dockerfile "
                f"nor a non-empty 'upstream' is present"
            )

    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _find_workdir() -> Path:
    """Locate the pipeline repo root (parent of ``containers/``).

    The script lives under ``aou-longread-sv-pipeline/scripts/``; the
    pipeline root is the parent of ``scripts/``. This keeps the CLI
    working regardless of the current shell directory.
    """
    return Path(__file__).resolve().parent.parent


def main(argv: Optional[List[str]] = None) -> int:
    del argv  # no args; kept for symmetry with other scripts/
    workdir = _find_workdir()
    containers_dir = workdir / "containers"
    manifest_path = containers_dir / "manifest.yaml"

    if not containers_dir.is_dir():
        print(f"[ERROR] {containers_dir} not found", file=sys.stderr)
        return 2

    dockerfiles = sorted(containers_dir.glob("*/Dockerfile"))
    print(f"[INFO] Linting {len(dockerfiles)} Dockerfile(s) under {containers_dir}")

    total_problems = 0
    for dockerfile in dockerfiles:
        tool = dockerfile.parent.name
        problems = lint_one_dockerfile(dockerfile, workdir)
        if problems:
            total_problems += len(problems)
            print(f"[FAIL] containers/{tool}/Dockerfile")
            for p in problems:
                print(f"       - {p}")
        else:
            print(f"[ OK ] containers/{tool}/Dockerfile")

    if manifest_path.exists():
        manifest_problems = lint_manifest(manifest_path, workdir)
        if manifest_problems:
            total_problems += len(manifest_problems)
            print("[FAIL] containers/manifest.yaml")
            for p in manifest_problems:
                print(f"       - {p}")
        else:
            print("[ OK ] containers/manifest.yaml")
    else:
        print(f"[WARN] {manifest_path} not found; skipping manifest round-trip check")

    if total_problems:
        print(f"[FAIL] {total_problems} problem(s) detected")
        return 1
    print(f"[PASS] all {len(dockerfiles)} Dockerfile(s) and manifest.yaml OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
