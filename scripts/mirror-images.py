#!/usr/bin/env python3
"""Mirror container images listed in ``containers/manifest.yaml`` into the
account's ap-southeast-1 ECR registry.

For every entry in the manifest this script either:

- builds the local Dockerfile at ``containers/<name>/Dockerfile`` with
  ``docker buildx`` when one exists, or
- pulls the upstream image with ``docker pull`` and retags it,

then tags the per-platform images against
``<account>.dkr.ecr.<region>.amazonaws.com/<ecr_repo>:<tag>``, pushes them,
combines them into a multi-arch manifest list when more than one platform is
listed, and reads the resulting per-platform image digests back via
``docker buildx imagetools inspect --raw`` so they can be written into
``containers/manifest.yaml`` and appended to ``SOURCES.md``'s
``## Image digests`` table.

Exits non-zero (1) if any image fails to mirror, naming the offending
image(s) in the final summary. Individual image failures do not abort the
rest of the run.

Requirements: 8.4, 8.5, 17.5, 17.6. Design: D4, D5.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import boto3
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REGION = "ap-southeast-1"
DEFAULT_MANIFEST_REL = "containers/manifest.yaml"
DEFAULT_SOURCES_REL = "SOURCES.md"
DEFAULT_HEALTHOMICS_CONFIG_REL = ".healthomics/config.toml"

IMAGE_DIGESTS_SECTION = "## Image digests"
TABLE_HEADER_ROW = "| Image | Platform | ECR URI | Digest | Mirrored at |"
TABLE_SEPARATOR_ROW = "|---|---|---|---|---|"

# Used only when --dry-run is set and we still want well-formed rows for
# local inspection; these are never written back into the manifest because
# --dry-run skips the rewrite entirely.
DRY_RUN_DIGEST_PLACEHOLDER = "sha256:" + ("0" * 64)


# ---------------------------------------------------------------------------
# Manifest / config helpers
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> dict:
    """Load ``containers/manifest.yaml`` and validate top-level shape."""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "images" not in data:
        raise ValueError(f"{path}: manifest missing top-level 'images' key")
    if not isinstance(data["images"], list) or not data["images"]:
        raise ValueError(f"{path}: 'images' must be a non-empty list")
    return data


def read_account_id_from_healthomics_config(config_path: Path) -> Optional[str]:
    """Pull ``account_id`` from ``.healthomics/config.toml`` if present.

    A tiny regex-based reader is sufficient here; the config file is a flat
    list of ``key = "value"`` pairs and we only need one scalar field. This
    keeps the script dependency-free on ``tomllib`` / ``tomli`` so it parses
    under both Python 3.9 (local dev) and 3.11 (containers, CI).
    """
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8")
    match = re.search(
        r'^\s*account_id\s*=\s*"([^"]+)"',
        text,
        flags=re.MULTILINE,
    )
    return match.group(1) if match else None


def resolve_account_id(cli_value: Optional[str], config_path: Path) -> str:
    if cli_value:
        return cli_value
    env_value = os.environ.get("AWS_ACCOUNT_ID")
    if env_value:
        return env_value
    config_value = read_account_id_from_healthomics_config(config_path)
    if config_value:
        return config_value
    raise SystemExit(
        "[ERROR] account_id not supplied; set --account-id, AWS_ACCOUNT_ID, "
        "or account_id in .healthomics/config.toml"
    )


def resolve_region(cli_value: Optional[str]) -> str:
    return cli_value or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run(cmd, *, capture_output: bool = False, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    """Thin wrapper around :func:`subprocess.run` with consistent options.

    Uses ``text=True`` for string stdio; callers pass ``check=False`` via
    ``CalledProcessError`` semantics directly through ``subprocess.run``.
    """
    return subprocess.run(
        cmd,
        capture_output=capture_output,
        text=True,
        check=True,
        input=stdin,
    )


# ---------------------------------------------------------------------------
# ECR helpers
# ---------------------------------------------------------------------------


def ecr_registry_host(account_id: str, region: str) -> str:
    return f"{account_id}.dkr.ecr.{region}.amazonaws.com"


def ecr_image_uri(account_id: str, region: str, ecr_repo: str, tag: str) -> str:
    return f"{ecr_registry_host(account_id, region)}/{ecr_repo}:{tag}"


def ecr_login(account_id: str, region: str) -> None:
    """Fetch an ECR login password and feed it to ``docker login``."""
    registry = ecr_registry_host(account_id, region)
    print(f"[INFO] Logging in to ECR registry {registry}")
    pw_result = _run(
        ["aws", "ecr", "get-login-password", "--region", region],
        capture_output=True,
    )
    _run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        stdin=pw_result.stdout.strip(),
    )


def ensure_ecr_repo(ecr_client, repo_name: str, *, dry_run: bool) -> None:
    """Create ``repo_name`` in ECR if missing.

    In ``--dry-run`` mode the describe call still happens (it's read-only);
    the create call is skipped.
    """
    try:
        ecr_client.describe_repositories(repositoryNames=[repo_name])
        print(f"[INFO] ECR repository {repo_name} already exists")
        return
    except ecr_client.exceptions.RepositoryNotFoundException:
        pass

    if dry_run:
        print(f"[DRY-RUN] would create ECR repository {repo_name}")
        return
    print(f"[INFO] Creating ECR repository {repo_name}")
    ecr_client.create_repository(
        repositoryName=repo_name,
        imageScanningConfiguration={"scanOnPush": True},
    )


# ---------------------------------------------------------------------------
# Docker build / pull / push helpers
# ---------------------------------------------------------------------------


def platform_suffix(platform: str) -> str:
    """``linux/amd64`` -> ``amd64``."""
    return platform.split("/", 1)[1]


def local_per_platform_tag(tool_name: str, tag: str, platform: str) -> str:
    return f"aou-sv-local/{tool_name}:{tag}-{platform_suffix(platform)}"


def per_platform_ecr_tag(base_uri: str, platform: str) -> str:
    """Tag used during per-platform push before manifest-list assembly."""
    return f"{base_uri}-{platform_suffix(platform)}"


def build_local_image(
    dockerfile_dir: Path,
    platform: str,
    local_tag: str,
    *,
    build_context: Optional[Path] = None,
) -> None:
    """Build ``dockerfile_dir/Dockerfile`` for ``platform``.

    ``build_context`` defaults to ``dockerfile_dir`` so existing single-
    directory images build unchanged. When a sibling marker file named
    ``BUILD_CONTEXT_REPO_ROOT`` is present next to the Dockerfile, callers
    pass the repo root instead so the Dockerfile can ``COPY`` files from
    outside the per-tool directory (e.g. metadata-writer bakes
    ``pricing/healthomics-ap-southeast-1.json`` into the image, and that
    file lives at the repo root per Design D9).
    """
    if build_context is None:
        build_context = dockerfile_dir
    print(
        f"[INFO] Building {local_tag} from {dockerfile_dir}/Dockerfile "
        f"({platform}, context={build_context})"
    )
    _run([
        "docker", "buildx", "build",
        "--platform", platform,
        "--provenance=false",
        "--sbom=false",
        "-f", str(dockerfile_dir / "Dockerfile"),
        "-t", local_tag,
        "--load",
        str(build_context),
    ])


def pull_and_tag_upstream(upstream: str, platform: str, local_tag: str) -> None:
    print(f"[INFO] Pulling {upstream} for {platform}")
    _run(["docker", "pull", "--platform", platform, upstream])
    _run(["docker", "tag", upstream, local_tag])


def push_per_platform(local_tag: str, per_platform_ref: str) -> None:
    print(f"[INFO] Tagging {local_tag} -> {per_platform_ref}")
    _run(["docker", "tag", local_tag, per_platform_ref])
    print(f"[INFO] Pushing {per_platform_ref}")
    _run(["docker", "push", per_platform_ref])


def assemble_manifest_list(base_uri: str, per_platform_refs: list) -> None:
    """Combine per-platform pushes into a manifest list at ``base_uri``.

    ``docker buildx imagetools create --tag <base_uri> <ref1> <ref2> ...``
    is safe for a single-entry list too, so we always take this path.
    """
    cmd = ["docker", "buildx", "imagetools", "create", "--tag", base_uri] + list(per_platform_refs)
    print(f"[INFO] Assembling manifest list at {base_uri}")
    _run(cmd)


def fetch_per_platform_digests(base_uri: str, platforms: list) -> dict:
    """Return ``{platform_suffix: 'sha256:...'}`` for ``base_uri``.

    Uses ``docker buildx imagetools inspect --raw`` and parses the returned
    OCI / Docker manifest-list JSON. For single-platform images (no manifest
    list), falls back to the image's own digest.
    """
    result = _run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", base_uri],
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    media_type = payload.get("mediaType", "")

    # Manifest-list or OCI index case: one entry per platform.
    if "manifest.list" in media_type or "image.index" in media_type or "manifests" in payload:
        digests: dict = {}
        for entry in payload.get("manifests", []):
            plat = entry.get("platform", {}) or {}
            os_name = plat.get("os")
            arch = plat.get("architecture")
            if os_name is None or arch is None:
                continue
            key = f"{os_name}/{arch}"
            if key in platforms:
                digests[platform_suffix(key)] = entry["digest"]
        missing = [p for p in platforms if platform_suffix(p) not in digests]
        if missing:
            raise RuntimeError(
                f"Manifest list at {base_uri} missing platforms: {missing}"
            )
        return digests

    # Single-platform case: run a non-raw inspect to surface the top-level
    # digest. There is exactly one platform listed for this entry per the
    # manifest schema.
    if len(platforms) != 1:
        raise RuntimeError(
            f"{base_uri} is a single-platform image but manifest lists "
            f"{len(platforms)} platforms; refusing to guess."
        )
    raw = _run(
        ["docker", "buildx", "imagetools", "inspect", base_uri],
        capture_output=True,
    ).stdout
    match = re.search(r"Digest:\s*(sha256:[0-9a-f]{64})", raw)
    if not match:
        raise RuntimeError(
            f"Could not parse digest from `buildx imagetools inspect` output for {base_uri}"
        )
    return {platform_suffix(platforms[0]): match.group(1)}


# ---------------------------------------------------------------------------
# Per-entry mirror flow
# ---------------------------------------------------------------------------


def mirror_entry(
    entry: dict,
    *,
    workdir: Path,
    account_id: str,
    region: str,
    ecr_client,
    dry_run: bool,
) -> dict:
    """Mirror one image entry. Returns ``{platform_suffix: digest}``."""
    name = entry["name"]
    ecr_repo = entry["ecr_repo"]
    tag = entry["tag"]
    platforms = list(entry["platforms"])
    upstream = entry.get("upstream", "")

    dockerfile_dir = workdir / "containers" / name
    has_local_dockerfile = (dockerfile_dir / "Dockerfile").exists()
    # Dockerfiles whose directory contains a BUILD_CONTEXT_REPO_ROOT marker
    # are built with the repo root as context so they can COPY files that
    # live outside their own directory (e.g. the metadata-writer image
    # bakes pricing/healthomics-ap-southeast-1.json at build time — Design
    # D9). All other images keep the per-tool directory as context so
    # their build context stays minimal.
    if has_local_dockerfile and (dockerfile_dir / "BUILD_CONTEXT_REPO_ROOT").exists():
        build_context: Path = workdir
    else:
        build_context = dockerfile_dir

    ensure_ecr_repo(ecr_client, ecr_repo, dry_run=dry_run)

    base_uri = ecr_image_uri(account_id, region, ecr_repo, tag)
    per_platform_refs: list = []

    for platform in platforms:
        local_tag = local_per_platform_tag(name, tag, platform)
        if has_local_dockerfile:
            build_local_image(
                dockerfile_dir,
                platform,
                local_tag,
                build_context=build_context,
            )
        else:
            pull_and_tag_upstream(upstream, platform, local_tag)

        if dry_run:
            print(
                f"[DRY-RUN] would push {local_tag} to "
                f"{per_platform_ecr_tag(base_uri, platform)}"
            )
            continue

        ref = per_platform_ecr_tag(base_uri, platform)
        push_per_platform(local_tag, ref)
        per_platform_refs.append(ref)

    if dry_run:
        return {
            platform_suffix(p): DRY_RUN_DIGEST_PLACEHOLDER for p in platforms
        }

    assemble_manifest_list(base_uri, per_platform_refs)
    return fetch_per_platform_digests(base_uri, platforms)


# ---------------------------------------------------------------------------
# Manifest digest rewrite
# ---------------------------------------------------------------------------


# Regexes module-level so unit tests (Task 2.3) can import and reuse them.
_NAME_LINE_RE = re.compile(r"^\s*-\s*name:\s*([A-Za-z0-9_\-]+)\s*$")
_DIGEST_LINE_RE = re.compile(
    r"^(?P<prefix>\s*digest_(?P<suffix>amd64|arm64):\s*)"
    r"(?P<digest>sha256:\S+)"
    r"(?P<trailing>\s*)$"
)


def rewrite_manifest_digests(manifest_path: Path, updates: dict) -> None:
    """Rewrite only ``digest_<platform>`` lines in-place, preserving all
    other bytes (comments, ordering, key names).

    ``updates`` is a mapping ``{(image_name, platform_suffix): digest}``. A
    line is rewritten iff its enclosing ``- name: <image>`` block is a key
    in ``updates``; everything else passes through verbatim. This is the
    "regex-replace only the digest portion" approach called out in the
    task's concrete guidance, which makes the rewrite trivially
    comment-preserving without needing ruamel.yaml.
    """
    text = manifest_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    current_name: Optional[str] = None
    for i, line in enumerate(lines):
        name_match = _NAME_LINE_RE.match(line.rstrip("\n"))
        if name_match:
            current_name = name_match.group(1)
            continue
        digest_match = _DIGEST_LINE_RE.match(line.rstrip("\n"))
        if digest_match and current_name is not None:
            suffix = digest_match.group("suffix")
            key = (current_name, suffix)
            if key in updates:
                newline = "\n" if line.endswith("\n") else ""
                lines[i] = (
                    digest_match.group("prefix")
                    + updates[key]
                    + digest_match.group("trailing")
                    + newline
                )

    new_text = "".join(lines)
    if new_text != text:
        manifest_path.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# SOURCES.md append
# ---------------------------------------------------------------------------


def append_sources_md_rows(sources_path: Path, rows: list) -> None:
    """Append Markdown rows under the ``## Image digests`` section.

    Inserts the table header and separator on first use; for subsequent
    runs, simply appends the new rows below the existing table. Other
    sections of ``SOURCES.md`` are left untouched.
    """
    if not rows:
        return
    if not sources_path.exists():
        raise SystemExit(
            f"[ERROR] {sources_path} does not exist; cannot append digest rows."
        )

    text = sources_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    try:
        section_idx = lines.index(IMAGE_DIGESTS_SECTION)
    except ValueError as exc:
        raise SystemExit(
            f"[ERROR] {sources_path} is missing the '{IMAGE_DIGESTS_SECTION}' "
            "section header; cannot append digest rows."
        ) from exc

    # The section ends at the next top-level H2 or EOF.
    end_idx = len(lines)
    for j in range(section_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end_idx = j
            break

    section = lines[section_idx:end_idx]
    has_header = (
        TABLE_HEADER_ROW in section and TABLE_SEPARATOR_ROW in section
    )

    insertion: list = []
    if not has_header:
        insertion.extend(["", TABLE_HEADER_ROW, TABLE_SEPARATOR_ROW])
    insertion.extend(rows)

    # Append after the last non-blank line in the section so the table sits
    # flush against existing content (or placeholder comments), then keep
    # any trailing blank lines that separated it from the next section.
    last_nonblank = section_idx
    for j in range(end_idx - 1, section_idx, -1):
        if lines[j].strip():
            last_nonblank = j
            break

    new_lines = (
        lines[: last_nonblank + 1]
        + insertion
        + lines[last_nonblank + 1 : end_idx]
        + lines[end_idx:]
    )
    trailer = "\n" if text.endswith("\n") else ""
    sources_path.write_text("\n".join(new_lines) + trailer, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirror-images.py",
        description=(
            "Mirror upstream container images (or build local Dockerfiles) "
            "into the account's ap-southeast-1 ECR registry, recording "
            "per-platform image digests back into containers/manifest.yaml "
            "and appending rows to SOURCES.md."
        ),
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST_REL,
        help="Path to the containers manifest (default: %(default)s).",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help=(
            "AWS account ID. Falls back to $AWS_ACCOUNT_ID, then account_id "
            "from .healthomics/config.toml."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region. Falls back to $AWS_DEFAULT_REGION, "
            f"then '{DEFAULT_REGION}'."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Perform all docker builds and pulls locally but do not create "
            "repos, push to ECR, rewrite the manifest, or update SOURCES.md. "
            "Used by Task 4.6 to validate Dockerfile buildability."
        ),
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated whitelist of tool names to mirror.",
    )
    parser.add_argument(
        "--skip",
        default=None,
        help="Comma-separated list of tool names to skip.",
    )
    return parser


def _parse_name_list(csv: Optional[str]) -> set:
    if csv is None:
        return set()
    return {piece.strip() for piece in csv.split(",") if piece.strip()}


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    # Repository root is the parent of ``containers/`` — which, for a
    # default ``containers/manifest.yaml`` invocation, is
    # ``manifest_path.parent.parent``.
    repo_root = manifest_path.parent.parent
    sources_path = repo_root / DEFAULT_SOURCES_REL
    healthomics_config = repo_root / DEFAULT_HEALTHOMICS_CONFIG_REL

    account_id = resolve_account_id(args.account_id, healthomics_config)
    region = resolve_region(args.region)

    manifest = load_manifest(manifest_path)
    entries = manifest["images"]

    only = _parse_name_list(args.only) or None
    skip = _parse_name_list(args.skip)
    selected = [
        entry for entry in entries
        if (only is None or entry["name"] in only) and entry["name"] not in skip
    ]
    if not selected:
        print("[WARN] No manifest entries selected after --only/--skip filtering.")
        return 0

    if not args.dry_run:
        ecr_login(account_id, region)
    ecr_client = boto3.client("ecr", region_name=region)

    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_updates: dict = {}
    sources_rows: list = []
    failures: list = []

    for entry in selected:
        name = entry["name"]
        print(f"[INFO] Mirroring {name}")
        try:
            digests = mirror_entry(
                entry,
                workdir=repo_root,
                account_id=account_id,
                region=region,
                ecr_client=ecr_client,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - surface every failure, keep going
            print(f"[ERROR] Failed to mirror {name}: {exc}", file=sys.stderr)
            failures.append(name)
            continue

        base_uri = ecr_image_uri(account_id, region, entry["ecr_repo"], entry["tag"])
        for suffix, digest in digests.items():
            all_updates[(name, suffix)] = digest
            sources_rows.append(
                f"| {name} | linux/{suffix} | {base_uri} | {digest} | {now_iso} |"
            )

    if args.dry_run:
        print("[DRY-RUN] skipping manifest rewrite and SOURCES.md append")
    elif all_updates:
        rewrite_manifest_digests(manifest_path, all_updates)
        append_sources_md_rows(sources_path, sources_rows)
        print(
            f"[INFO] Rewrote {len(all_updates)} digest entries in {manifest_path}"
        )
        print(
            f"[INFO] Appended {len(sources_rows)} rows to "
            f"{sources_path}#image-digests"
        )

    if failures:
        print(
            f"[ERROR] {len(failures)} image(s) failed to mirror: "
            f"{', '.join(failures)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
