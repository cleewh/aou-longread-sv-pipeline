#!/usr/bin/env python3
"""Rewrite every `runtime.docker` sentinel-digest in wdl/ against containers/manifest.yaml.

Idempotent sidekick to `mirror-images.py` — useful when the manifest already
carries real digests (e.g. after reading them from ECR) but the WDL task files
still carry the sentinel. The WDL does not have per-task ARG for which
platform; we choose arm64 where available, else amd64, matching the Design
§Graviton matrix preference for Graviton instances.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "containers" / "manifest.yaml"
WDL_DIR = REPO / "wdl"
ACCOUNT_ID = "687677765589"
REGION = "ap-southeast-1"
REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"

# Map tool name → WDL task files that reference it. This is a narrow helper;
# a task file that mentions multiple images (e.g. harmoniser.wdl only uses
# harmoniser) is handled by the per-repo mapping.
TOOL_TO_TASKS = {
    "input-validator": ["input_validator.wdl"],  # maps to metadata-writer manifest entry
    "hifiasm": ["hifiasm.wdl"],
    "pbmm2": ["pbmm2.wdl"],
    "pav": ["pav.wdl"],
    "pav2svs": ["pav2svs.wdl"],
    "sniffles2": ["sniffles2.wdl"],
    "pbsv": ["pbsv.wdl"],
    "harmoniser": ["harmoniser.wdl"],
    "metadata-writer": ["metadata_writer.wdl", "input_validator.wdl"],
}


def preferred_digest(entry: dict) -> str:
    """Prefer amd64 to match HealthOmics `omics.c.*` default x86_64 instance families.

    The Design Graviton matrix preferred arm64 when upstream supported it, but
    HealthOmics ap-southeast-1 defaults to x86_64 compute unless a Graviton
    family is explicitly requested via resource_overrides. Until the instance
    selector routes arm64-capable tasks to a Graviton family, emit amd64 for
    every task so images match their host architecture and tasks don't die at
    container start.
    """
    if "digest_amd64" in entry and not entry["digest_amd64"].endswith(
        "TO_BE_FILLED_BY_MIRROR_IMAGES_PY"
    ):
        return entry["digest_amd64"]
    return entry["digest_arm64"]


def main() -> int:
    manifest = yaml.safe_load(MANIFEST.read_text())
    # index by ecr_repo basename (aou-sv/<tool> → <tool>)
    by_repo = {
        entry["ecr_repo"].split("/")[-1]: entry for entry in manifest["images"]
    }

    # Each WDL task references a single tool image; match by grep-substring.
    wdl_files = sorted((WDL_DIR / "tasks").glob("*.wdl"))
    total_replacements = 0
    for wdl in wdl_files:
        text = wdl.read_text()
        original = text
        for tool, entry in by_repo.items():
            ecr_uri_re = re.compile(
                rf"{re.escape(REGISTRY)}/aou-sv/{re.escape(tool)}"
                r"@sha256:[0-9a-f]{64}"
            )

            def repl(m, _entry=entry, _tool=tool):
                return (
                    f"{REGISTRY}/aou-sv/{_tool}"
                    f"@{preferred_digest(_entry)}"
                )

            new_text, count = ecr_uri_re.subn(repl, text)
            if count:
                text = new_text
                total_replacements += count
        if text != original:
            wdl.write_text(text)
            print(f"[UPDATE] {wdl.relative_to(REPO)}")
    print(f"[OK] rewrote {total_replacements} docker URI(s) across {len(wdl_files)} WDL file(s)")
    return 0 if total_replacements > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
