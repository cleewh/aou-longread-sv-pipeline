# Task 10.7: Cost-optimal HealthOmics instance selector.
"""Pick the cheapest HealthOmics instance that meets a resource request.

Requirements: 17.1, 17.3
Design: D13, Property 15.

Property 15: *for any task resource request (cpu, memory_gb, disk_gb) with
positive integer values, the instance selected by the instance-selection
function SHALL satisfy instance.cpu >= cpu AND instance.memory_gb >=
memory_gb AND instance.disk_gb >= disk_gb, AND no other instance in the
ap-southeast-1 HealthOmics price list with a strictly lower hourly price SHALL
satisfy all three constraints.*

HealthOmics does not bind local SSD to a per-instance specification the way
EC2 does — run storage is charged separately (``run_storage_usd_per_gb_hour``)
and is provisioned via ``storageType`` / ``storageCapacity`` on StartRun. We
still accept ``disk_gb`` in the signature for Property 15 / Requirement 17.3
compatibility and because the price list may grow a ``local_ssd_gb`` field in
a future revision; if the price list *does* carry a ``local_ssd_gb`` per-spec
field, this selector enforces it as a real bound. Otherwise ``disk_gb`` is
satisfied by the separate run-storage dimension and does not constrain
instance choice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class InstanceType:
    """A single HealthOmics instance offering."""

    name: str
    cpu: int
    memory_gb: int
    hourly_usd: float
    local_ssd_gb: int | None = None


def load_price_list(path: Path | str) -> dict:
    """Read a HealthOmics price-list JSON file from disk."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _coerce_specs(price_list: Mapping[str, object]) -> list[InstanceType]:
    specs = price_list.get("instance_specs", {})
    prices = price_list.get("instance_usd_per_hour", {})
    if not isinstance(specs, Mapping) or not isinstance(prices, Mapping):
        raise ValueError(
            "price_list must contain 'instance_specs' and 'instance_usd_per_hour' mappings"
        )
    instances: list[InstanceType] = []
    for name, spec in specs.items():
        if name not in prices:
            continue
        if not isinstance(spec, Mapping):
            continue
        cpu = spec.get("cpu")
        memory_gb = spec.get("memory_gb")
        if not isinstance(cpu, int) or not isinstance(memory_gb, int):
            continue
        if cpu <= 0 or memory_gb <= 0:
            continue
        hourly = prices[name]
        if not isinstance(hourly, (int, float)) or hourly <= 0:
            continue
        local_ssd_raw = spec.get("local_ssd_gb")
        local_ssd = (
            int(local_ssd_raw)
            if isinstance(local_ssd_raw, int) and local_ssd_raw > 0
            else None
        )
        instances.append(
            InstanceType(
                name=name,
                cpu=cpu,
                memory_gb=memory_gb,
                hourly_usd=float(hourly),
                local_ssd_gb=local_ssd,
            )
        )
    return instances


def select_instance(
    cpu: int,
    memory_gb: int,
    disk_gb: int,
    price_list: Mapping[str, object],
) -> InstanceType:
    """Return the lowest-price instance satisfying the resource bounds.

    Bounds: ``instance.cpu >= cpu`` AND ``instance.memory_gb >= memory_gb``.
    When an instance spec carries an explicit ``local_ssd_gb`` (optional in
    the price-list schema), it must also satisfy ``local_ssd_gb >= disk_gb``;
    otherwise ``disk_gb`` is treated as satisfied by HealthOmics run storage
    and does not constrain the pick (Design D13 rationale).

    Ties on hourly price are broken by: smaller cpu → smaller memory_gb →
    instance name (lexicographic), purely for determinism — Property 15 only
    requires optimality, so any deterministic tie-break is valid.
    """
    if not isinstance(cpu, int) or isinstance(cpu, bool) or cpu <= 0:
        raise ValueError(f"select_instance: cpu must be a positive int (got {cpu!r})")
    if not isinstance(memory_gb, int) or isinstance(memory_gb, bool) or memory_gb <= 0:
        raise ValueError(
            f"select_instance: memory_gb must be a positive int (got {memory_gb!r})"
        )
    if not isinstance(disk_gb, int) or isinstance(disk_gb, bool) or disk_gb <= 0:
        raise ValueError(
            f"select_instance: disk_gb must be a positive int (got {disk_gb!r})"
        )

    candidates: list[InstanceType] = []
    for inst in _coerce_specs(price_list):
        if inst.cpu < cpu or inst.memory_gb < memory_gb:
            continue
        if inst.local_ssd_gb is not None and inst.local_ssd_gb < disk_gb:
            continue
        candidates.append(inst)
    if not candidates:
        raise ValueError(
            f"select_instance: no HealthOmics instance satisfies "
            f"cpu>={cpu}, memory_gb>={memory_gb}, disk_gb>={disk_gb}"
        )
    candidates.sort(
        key=lambda i: (i.hourly_usd, i.cpu, i.memory_gb, i.name)
    )
    return candidates[0]
