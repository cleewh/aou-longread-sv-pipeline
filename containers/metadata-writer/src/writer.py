"""MetadataWriter_Task entry point (Task 3.8).

Assembles the per-run ``run_metadata.json`` document described in
Design §Data Models / Requirements 7.2, 12.2, 16.2, 17.10. The writer
is the final task in the HealthOmics workflow (Design D8); it reads
per-caller status files, tool version strings, image digests, and the
HealthOmics run identifiers, then bolts on the ``Cost_Report``
(Design D9) produced by :mod:`cost_report` and validates the whole
document against the bundled JSON schema before writing
``<sample_id>.run_metadata.json`` to the current working directory.

Public surface:

  * :func:`build_run_metadata` — assemble the metadata dict from Python
    arguments (primary unit-testable API; exercised by the property
    test in ``test/property/test_writer_property.py``).
  * :func:`validate_schema` — validate a metadata dict against the
    bundled schema, re-raising with the offending path on failure.
  * :func:`main` — CLI entry point wired up in :mod:`__main__`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

import jsonschema

# Support both package-style ("from metadata_writer import writer") and
# top-level module import (test/conftest.py puts ``src/`` on sys.path).
try:  # pragma: no cover - exercised in container, not in tests
    from . import cost_report
except ImportError:
    import cost_report  # type: ignore[no-redef]


# --- Schema locations -------------------------------------------------------

#: Path to the bundled schema when running inside the container image
#: (Dockerfile copies the ``src/`` tree to
#: ``/opt/metadata-writer/metadata_writer/``).
DEFAULT_CONTAINER_SCHEMA_PATH = Path(
    "/opt/metadata-writer/metadata_writer/run_metadata.schema.json"
)

#: Repo-local location of the schema (sibling of this module).
DEFAULT_REPO_SCHEMA_PATH = Path(__file__).resolve().parent / "run_metadata.schema.json"


# --- Tools we record in run_metadata.json ----------------------------------

_REQUIRED_TOOLS: tuple[str, ...] = (
    "hifiasm",
    "pav",
    "pav2svs",
    "sniffles2",
    "pbsv",
    "pbmm2",
    "harmoniser",
)

_REQUIRED_CALLER_KEYS: tuple[str, ...] = (
    "hifiasm_pav",
    "sniffles2",
    "pbsv",
    "harmoniser",
)


# --- Helpers ---------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of ``path``'s bytes."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_schema(schema_path: Optional[Path | str]) -> dict:
    """Load the bundled run_metadata schema.

    Resolution order: explicit ``schema_path`` → container default →
    repo-local default. Raises :class:`FileNotFoundError` naming every
    tried path when none exist.
    """
    tried: list[Path] = []
    if schema_path is not None:
        candidate = Path(schema_path)
        tried.append(candidate)
        if candidate.is_file():
            with candidate.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        raise FileNotFoundError(f"schema not found at: {candidate}")

    for candidate in (DEFAULT_CONTAINER_SCHEMA_PATH, DEFAULT_REPO_SCHEMA_PATH):
        tried.append(candidate)
        if candidate.is_file():
            with candidate.open("r", encoding="utf-8") as handle:
                return json.load(handle)

    tried_str = ", ".join(str(p) for p in tried)
    raise FileNotFoundError(f"no run_metadata.schema.json found; tried: {tried_str}")


# --- Public API ------------------------------------------------------------


def build_run_metadata(
    *,
    pipeline_version: str,
    git_commit: str,
    input_manifest_path: Path | str,
    region: str,
    healthomics_run_id: str,
    workflow_id: str,
    workflow_name: str,
    workflow_version: str,
    start_time: str,
    end_time: str,
    status: str,
    storage_type: str,
    per_caller_status: dict,
    tool_info: dict,
    outputs: dict,
    cost_records: Sequence[dict],
    pricing: Optional[dict] = None,
) -> dict:
    """Assemble the ``run_metadata.json`` document as a Python dict.

    Reads the Input_Manifest from disk twice: once as bytes to compute
    the SHA-256 for ``pipeline.input_manifest_sha256`` (Design §Data
    Models), once as JSON to echo under the ``input_manifest`` top-level
    key (Requirement 7.2).

    Builds the ``Cost_Report`` via :func:`cost_report.build_cost_report`
    using ``pricing`` if supplied, otherwise :func:`cost_report.load_pricing`.
    """
    manifest_path = Path(input_manifest_path)
    manifest_sha = _sha256_file(manifest_path)
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest_echo = json.load(handle)

    if pricing is None:
        pricing = cost_report.load_pricing()
    cost_block = cost_report.build_cost_report(list(cost_records), pricing)

    return {
        "pipeline": {
            "version": pipeline_version,
            "git_commit": git_commit,
            "input_manifest_sha256": manifest_sha,
            "region": region,
        },
        "run": {
            "healthomics_run_id": healthomics_run_id,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "workflow_version": workflow_version,
            "start_time": start_time,
            "end_time": end_time,
            "status": status,
            "storage_type": storage_type,
        },
        "input_manifest": manifest_echo,
        "tools": {tool: dict(tool_info[tool]) for tool in _REQUIRED_TOOLS},
        "per_caller_status": {
            key: per_caller_status[key] for key in _REQUIRED_CALLER_KEYS
        },
        "outputs": dict(outputs),
        "Cost_Report": cost_block,
    }


def validate_schema(metadata: dict, schema_path: Optional[Path | str] = None) -> None:
    """Validate ``metadata`` against the bundled schema.

    Re-raises :class:`jsonschema.ValidationError` with an annotated
    message that includes the JSON pointer to the offending field for
    easier debugging from CloudWatch logs. The raised exception keeps
    its original traceback so structured tooling can still introspect.
    """
    schema = _load_schema(schema_path)
    try:
        jsonschema.validate(instance=metadata, schema=schema)
    except jsonschema.ValidationError as exc:
        pointer = "/" + "/".join(str(p) for p in exc.absolute_path)
        # Rewrap with the path in the message but preserve the original
        # exception type so callers can still catch ValidationError.
        raise jsonschema.ValidationError(
            f"run_metadata.json schema validation failed at {pointer}: {exc.message}",
            validator=exc.validator,
            path=exc.absolute_path,
            schema_path=exc.absolute_schema_path,
            cause=exc.cause,
            context=exc.context,
            instance=exc.instance,
            schema=exc.schema,
        ) from exc


# --- CLI entry point -------------------------------------------------------


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Write ``<sample_id>.run_metadata.json`` to the current directory.

    All inputs are passed as flags or file paths so the task is pure and
    side-effect-free aside from the single output file. Returns:

    * ``0`` on success,
    * ``2`` on schema validation failure,
    * non-zero (argparse default) on malformed CLI.
    """
    parser = argparse.ArgumentParser(
        prog="metadata-writer write",
        description="Assemble run_metadata.json for a completed HealthOmics run.",
    )
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--pipeline-version", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--input-manifest", required=True, help="Path to Input_Manifest JSON.")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--healthomics-run-id", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--workflow-version", required=True)
    parser.add_argument("--start-time", required=True)
    parser.add_argument("--end-time", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--storage-type", choices=("DYNAMIC", "STATIC"), default="DYNAMIC")
    parser.add_argument(
        "--per-caller-status",
        required=True,
        help="Path to JSON with keys hifiasm_pav, sniffles2, pbsv, harmoniser.",
    )
    parser.add_argument(
        "--tool-info",
        required=True,
        help="Path to JSON mapping tool name -> {version, image_digest}.",
    )
    parser.add_argument(
        "--outputs",
        required=True,
        help="Path to JSON mapping output name -> s3 URI (or null).",
    )
    parser.add_argument(
        "--cost-records",
        required=True,
        help="Path to JSON list of task cost records.",
    )
    parser.add_argument(
        "--pricing",
        default=None,
        help="Optional path to pricing JSON (default: baked-in/repo-local).",
    )
    args = parser.parse_args(argv if argv is None else list(argv))

    per_caller_status = _read_json(args.per_caller_status)
    tool_info = _read_json(args.tool_info)
    outputs = _read_json(args.outputs)
    cost_records = _read_json(args.cost_records)
    pricing = cost_report.load_pricing(args.pricing)

    metadata = build_run_metadata(
        pipeline_version=args.pipeline_version,
        git_commit=args.git_commit,
        input_manifest_path=args.input_manifest,
        region=args.region,
        healthomics_run_id=args.healthomics_run_id,
        workflow_id=args.workflow_id,
        workflow_name=args.workflow_name,
        workflow_version=args.workflow_version,
        start_time=args.start_time,
        end_time=args.end_time,
        status=args.status,
        storage_type=args.storage_type,
        per_caller_status=per_caller_status,
        tool_info=tool_info,
        outputs=outputs,
        cost_records=cost_records,
        pricing=pricing,
    )

    try:
        validate_schema(metadata)
    except jsonschema.ValidationError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        _emit_task_trailer("metadata_writer", 2)
        return 2

    out_path = Path.cwd() / f"{args.sample_id}.run_metadata.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(str(out_path))
    _emit_task_trailer("metadata_writer", 0)
    return 0


# ---------------------------------------------------------------------------
# Task 19.1 — stdout trailer pattern (Design §Error Handling Layer 3).
# ---------------------------------------------------------------------------
# Every task we own emits a single JSON line of the form
# {"task": "<name>", "status": "ok"|"error", "exit_code": N,
#  "stderr_tail": "<last ~100 lines of stderr>"} to stdout immediately
# before exit. `MetadataWriter_Task` consumes the aggregated trailers
# from each upstream caller to build `per_caller_status` without
# re-reading CloudWatch. `stderr_tail` is left empty at this Python
# layer; the WDL wrapper is responsible for splicing in the real tail.
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
