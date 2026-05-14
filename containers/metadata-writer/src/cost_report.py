"""Cost_Report arithmetic (Task 3.6).

Implements Requirement 17.10 and Design D9 / the ``Cost_Report`` section
of the ``run_metadata.json`` schema. The public surface matches the
stub shipped in Task 3.1 so the dispatcher in :mod:`__main__` and the
writer in :mod:`writer` can drop this in without changing call sites.

Formula (Property 19)::

    estimated_usd(record) =
        instance_usd_per_hour[record.instance_type]
          * max(record.cpu_hours, record.memory_gb_hours)
      + run_storage_usd_per_gb_hour[record.storage_type]
          * record.storage_gb_hours

The price list is baked into the container at build time
(``/opt/pricing/healthomics-ap-southeast-1.json``, Design D9). When the
module is imported from a local development / test environment the
container path does not exist; :func:`load_pricing` falls back to the
repo-local copy at ``pricing/healthomics-ap-southeast-1.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence


# --- Pricing file locations -------------------------------------------------

#: Location of the baked-in price list inside the metadata-writer image
#: (Design D9, Dockerfile ``COPY`` step).
DEFAULT_CONTAINER_PRICING_PATH = Path("/opt/pricing/healthomics-ap-southeast-1.json")

#: Repo-local copy of the same price list used by pytest and any local
#: development invocation. The file lives at
#: ``aou-longread-sv-pipeline/pricing/healthomics-ap-southeast-1.json``
#: — four levels up from this source file:
#: ``containers/metadata-writer/src/cost_report.py`` -> src -> metadata-writer
#: -> containers -> repo root.
DEFAULT_REPO_PRICING_PATH = (
    Path(__file__).resolve().parents[3]
    / "pricing"
    / "healthomics-ap-southeast-1.json"
)


# --- Pricing loading --------------------------------------------------------


def load_pricing(path: Optional[Path | str] = None) -> dict:
    """Load the HealthOmics price list from disk.

    Resolution order:

      1. ``path`` if supplied (as :class:`pathlib.Path` or ``str``).
      2. :data:`DEFAULT_CONTAINER_PRICING_PATH` if it exists (inside the
         metadata-writer container image).
      3. :data:`DEFAULT_REPO_PRICING_PATH` for local / test runs.

    Raises :class:`FileNotFoundError` with a message naming every path
    that was tried when none exist.
    """
    tried: list[Path] = []
    if path is not None:
        candidate = Path(path)
        tried.append(candidate)
        if candidate.is_file():
            with candidate.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        raise FileNotFoundError(
            f"pricing file not found at requested path: {candidate}"
        )

    for candidate in (DEFAULT_CONTAINER_PRICING_PATH, DEFAULT_REPO_PRICING_PATH):
        tried.append(candidate)
        if candidate.is_file():
            with candidate.open("r", encoding="utf-8") as handle:
                return json.load(handle)

    tried_str = ", ".join(str(p) for p in tried)
    raise FileNotFoundError(
        f"no pricing file found; tried: {tried_str}"
    )


# --- Cost arithmetic --------------------------------------------------------


def compute_task_cost(record: dict, pricing: dict) -> float:
    """Return the estimated USD cost for a single task execution record.

    Applies the Property 19 formula::

        instance_usd_per_hour[instance_type]
            * max(cpu_hours, memory_gb_hours)
          + run_storage_usd_per_gb_hour[storage_type]
            * storage_gb_hours

    ``record`` must carry ``instance_type``, ``cpu_hours``,
    ``memory_gb_hours``, and ``storage_gb_hours``. ``storage_type``
    defaults to ``"DYNAMIC"`` when absent (per Design D6).

    Raises :class:`KeyError` with a message naming the unknown instance
    or storage type when the pricing table does not cover the record.
    """
    instance_type = record["instance_type"]
    try:
        rate = pricing["instance_usd_per_hour"][instance_type]
    except KeyError as exc:
        raise KeyError(
            f"unknown instance_type in pricing table: {instance_type!r}"
        ) from exc

    storage_type = record.get("storage_type", "DYNAMIC")
    try:
        storage_rate = pricing["run_storage_usd_per_gb_hour"][storage_type]
    except KeyError as exc:
        raise KeyError(
            f"unknown storage_type in pricing table: {storage_type!r}"
        ) from exc

    cpu_hours = record["cpu_hours"]
    memory_gb_hours = record["memory_gb_hours"]
    storage_gb_hours = record["storage_gb_hours"]

    return rate * max(cpu_hours, memory_gb_hours) + storage_rate * storage_gb_hours


def compute_total(records: Iterable[dict], pricing: dict) -> float:
    """Return the sum of :func:`compute_task_cost` over ``records``."""
    return sum(compute_task_cost(record, pricing) for record in records)


def build_cost_report(records: Sequence[dict], pricing: dict) -> dict:
    """Assemble a ``Cost_Report`` object for ``run_metadata.json``.

    The returned dict conforms to the Design §Data Models
    ``run_metadata.json`` schema's ``Cost_Report`` subsection:

    * ``pricing_source`` — string of the form
      ``healthomics-ap-southeast-1.json@<sha256>``.
    * ``tasks`` — one entry per record, carrying the record's fields plus
      an ``estimated_usd`` key.
    * ``total_estimated_usd`` — sum of the per-task estimates.
    """
    sha = pricing.get("pricing_source_sha256", "unknown")
    task_entries: list[dict] = []
    for record in records:
        entry = dict(record)
        entry["estimated_usd"] = compute_task_cost(record, pricing)
        task_entries.append(entry)
    return {
        "pricing_source": f"healthomics-ap-southeast-1.json@{sha}",
        "tasks": task_entries,
        "total_estimated_usd": compute_total(records, pricing),
    }


# --- CLI entry point --------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Compute a ``Cost_Report`` from a JSON records file.

    Reads the records JSON (a list of task-execution records) from
    ``--records <path>``, loads the price list from ``--pricing <path>``
    (falling back to the baked-in / repo-local copies), and writes the
    assembled ``Cost_Report`` JSON to stdout. Returns 0 on success.
    """
    parser = argparse.ArgumentParser(
        prog="metadata-writer compute-cost",
        description=(
            "Compute the Cost_Report section of run_metadata.json from a "
            "list of per-task execution records."
        ),
    )
    parser.add_argument(
        "--records",
        required=True,
        help="Path to a JSON file containing a list of task records.",
    )
    parser.add_argument(
        "--pricing",
        default=None,
        help=(
            "Optional path to a pricing JSON. Defaults to the baked-in "
            "container copy and then the repo-local copy."
        ),
    )
    args = parser.parse_args(argv if argv is None else list(argv))

    with open(args.records, "r", encoding="utf-8") as handle:
        records = json.load(handle)
    if not isinstance(records, list):
        print("error: --records file must contain a JSON list", file=sys.stderr)
        _emit_task_trailer("cost_report", 2)
        return 2

    pricing = load_pricing(args.pricing)
    report = build_cost_report(records, pricing)
    print(json.dumps(report, separators=(",", ":")))
    _emit_task_trailer("cost_report", 0)
    return 0


# ---------------------------------------------------------------------------
# Task 19.1 — stdout trailer pattern (Design §Error Handling Layer 3).
# ---------------------------------------------------------------------------
# See writer.py for the full contract. Every module we own emits a
# single JSON line of the form
# {"task": "<name>", "status": "ok"|"error", "exit_code": N,
#  "stderr_tail": "..."} to stdout just before exit; MetadataWriter_Task
# aggregates the trailers to populate `per_caller_status`.
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
