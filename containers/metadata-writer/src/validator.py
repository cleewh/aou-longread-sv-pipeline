"""Input_Manifest validator (Task 3.2).

Implements Requirements 2.1, 2.3, 2.4, 2.5, 2.6, 6.5 and the
InputValidator_Task contract from the Design document. The full
behavioural contract — in particular the error-message format and the
check ordering — is also exercised by the Hypothesis property test in
``test/property/test_validator_property.py`` (Property 1: Input_Manifest
validator is sound and complete).

Public surface (stable, matches the stub shipped in Task 3.1):

  * ``validate(manifest: dict) -> tuple[bool, str]``
  * ``main(argv: Sequence[str] | None = None) -> int``

The validator is deliberately pure Python / no I/O so it can be reused
from the WDL task, from the client-side ``submit-run.py``, and from the
test suite without a container round-trip.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Sequence


# --- Public constants -------------------------------------------------------

#: Required fields per Requirement 2.1 / Design §Input_Manifest schema.
REQUIRED_FIELDS: tuple[str, ...] = (
    "sample_id",
    "hifi_reads_bam",
    "reference_fasta",
    "reference_fai",
    "output_prefix",
)

#: Sample-id regex per Requirement 2.5.
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Per-caller toggles checked for the "all disabled" condition (Req 2.6 /
#: Design: not-all-callers-disabled rule).
_CALLER_FLAGS: tuple[str, ...] = (
    "run_hifiasm_pav",
    "run_sniffles2",
    "run_pbsv",
)


# --- Internal helpers -------------------------------------------------------


def _offending_chars(sample_id: str) -> list[str]:
    """Return the sorted list of distinct characters in ``sample_id`` that
    are outside ``[A-Za-z0-9_-]``. Used to build the Requirement 2.5
    diagnostic message.
    """
    seen: set[str] = set()
    for ch in sample_id:
        if not re.match(r"[A-Za-z0-9_-]", ch):
            seen.add(ch)
    return sorted(seen)


# --- Public API -------------------------------------------------------------


def validate(manifest: dict) -> tuple[bool, str]:
    """Return ``(is_valid, error_message)`` for an Input_Manifest dict.

    On success returns ``(True, "")``. On any failure returns
    ``(False, "<field>: <reason>")``. Checks run in the order documented
    in Design §Layer 2 error table so that diagnostics name the earliest
    offending field.

    Requirements: 2.1, 2.3, 2.4, 2.5, 2.6, 6.5.
    """
    if not isinstance(manifest, dict):
        return False, "manifest: must be a JSON object"

    # 1. Required fields must be present.
    for field in REQUIRED_FIELDS:
        if field not in manifest:
            return False, f"{field}: missing required field"

    # 2. Required fields must be non-empty strings.
    for field in REQUIRED_FIELDS:
        value = manifest[field]
        if not isinstance(value, str) or value == "":
            return False, f"{field}: empty required field"

    # 3. sample_id charset (Req 2.5).
    sample_id = manifest["sample_id"]
    if not SAMPLE_ID_RE.match(sample_id):
        offenders = _offending_chars(sample_id)
        offender_repr = ", ".join(repr(c) for c in offenders)
        return False, (
            f"sample_id: contains characters outside [A-Za-z0-9_-]: "
            f"{offender_repr}"
        )

    # 4. output_prefix must end with '/' (Design: Input_Manifest schema).
    output_prefix = manifest["output_prefix"]
    if not output_prefix.endswith("/"):
        return False, "output_prefix: must end with '/'"

    # 5. Aligned-BAM requires a BAM index (Req 2.3).
    if manifest.get("hifi_reads_aligned"):
        bai = manifest.get("hifi_reads_bai")
        if not isinstance(bai, str) or bai == "":
            return False, (
                "hifi_reads_bai: required when hifi_reads_aligned is true"
            )

    # 6. Not-all-callers-disabled (Req 2.6 / 6.5 interplay).
    if all(flag in manifest for flag in _CALLER_FLAGS):
        if not any(manifest[flag] for flag in _CALLER_FLAGS):
            return False, (
                "callers: at least one of "
                + ", ".join(_CALLER_FLAGS)
                + " must be true"
            )

    return True, ""


def _read_manifest(path: str | None) -> dict:
    """Load a manifest from a filesystem path or from stdin."""
    if path is None:
        raw = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    return json.loads(raw)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the ``validate`` subcommand.

    Reads an Input_Manifest JSON either from ``--manifest <path>`` or
    from stdin, runs :func:`validate`, prints a single-line JSON result
    to stdout of the form ``{"valid": bool, "error": str}``, and exits
    ``0`` on valid / ``2`` on invalid.

    Returns the integer exit code so unit tests can call ``main([...])``
    without monkeypatching ``sys.exit``.
    """
    parser = argparse.ArgumentParser(
        prog="metadata-writer validate",
        description=(
            "Validate an Input_Manifest JSON document. Reads JSON from "
            "--manifest <path> or from stdin."
        ),
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to a JSON Input_Manifest (default: read from stdin)",
    )
    args = parser.parse_args(argv if argv is None else list(argv))

    try:
        manifest = _read_manifest(args.manifest)
    except json.JSONDecodeError as exc:
        result = {"valid": False, "error": f"manifest: invalid JSON: {exc}"}
        print(json.dumps(result, separators=(",", ":")))
        return 2
    except OSError as exc:
        result = {"valid": False, "error": f"manifest: {exc}"}
        print(json.dumps(result, separators=(",", ":")))
        return 2

    is_valid, error = validate(manifest)
    result = {"valid": is_valid, "error": error}
    print(json.dumps(result, separators=(",", ":")))
    exit_code = 0 if is_valid else 2
    _emit_task_trailer("validator", exit_code)
    return exit_code


# ---------------------------------------------------------------------------
# Task 19.1 — stdout trailer pattern (Design §Error Handling Layer 3).
# ---------------------------------------------------------------------------
# MetadataWriter_Task parses a per-task trailer of the form
# {"task": "<name>", "status": "ok"|"error", "exit_code": N,
#  "stderr_tail": "<last ~100 lines of stderr>"} emitted to stdout by
# every task wrapper immediately before exit. The trailer is the
# authoritative source of `per_caller_status` in run_metadata.json.
#
# We emit it unconditionally here as a single JSON line right before the
# CLI returns so that WDL task wrappers reading the validator's stdout
# can recover both the validator payload (first JSON line) and the
# structured task trailer (last JSON line). `stderr_tail` is left empty
# at this layer; the WDL task wrapper is responsible for appending the
# real stderr tail when it aggregates task trailers for
# MetadataWriter_Task.
def _emit_task_trailer(task_name: str, exit_code: int) -> None:
    """Emit the Design §Layer 3 stdout trailer."""
    trailer = {
        "task": task_name,
        "status": "ok" if exit_code == 0 else "error",
        "exit_code": int(exit_code),
        "stderr_tail": "",
    }
    print(json.dumps(trailer, separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover - exercised via CLI invocation
    sys.exit(main())
