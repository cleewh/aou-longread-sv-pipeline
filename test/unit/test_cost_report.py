"""Unit tests for :mod:`cost_report` (Task 3.6).

Complements the Hypothesis property tests in
``test/property/test_cost_report_property.py`` with concrete examples
for the pricing loader fallback, the Cost_Report structure, and error
paths for unknown instance / storage types.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cost_report  # noqa: E402 — sys.path patched by test/conftest.py


_PRICING = {
    "pricing_source_sha256": "deadbeef" + "0" * 56,
    "instance_usd_per_hour": {"omics.c.4xlarge": 1.36, "omics.m.2xlarge": 0.76},
    "run_storage_usd_per_gb_hour": {"DYNAMIC": 0.000137, "STATIC": 0.000206},
}


def test_load_pricing_repo_local_default() -> None:
    """With no explicit path and no container copy, loads the repo-local
    pricing file shipped at ``pricing/healthomics-ap-southeast-1.json``."""
    pricing = cost_report.load_pricing()
    assert pricing["region"] == "ap-southeast-1"
    assert "omics.c.4xlarge" in pricing["instance_usd_per_hour"]
    assert "DYNAMIC" in pricing["run_storage_usd_per_gb_hour"]


def test_load_pricing_explicit_missing_path_raises(tmp_path: Path) -> None:
    """An explicit path that does not exist raises FileNotFoundError
    naming the missing path."""
    missing = tmp_path / "nonexistent.json"
    with pytest.raises(FileNotFoundError, match=str(missing)):
        cost_report.load_pricing(missing)


def test_compute_task_cost_known_example() -> None:
    """Formula: 1.36 * max(2, 8) + 0.000137 * 100 = 10.8937."""
    record = {
        "task_name": "pav",
        "instance_type": "omics.c.4xlarge",
        "cpu_hours": 2.0,
        "memory_gb_hours": 8.0,
        "storage_gb_hours": 100.0,
        "storage_type": "DYNAMIC",
    }
    result = cost_report.compute_task_cost(record, _PRICING)
    assert result == pytest.approx(1.36 * 8.0 + 0.000137 * 100.0)


def test_compute_task_cost_defaults_to_dynamic_storage() -> None:
    """When ``storage_type`` is absent the rate resolves to DYNAMIC."""
    record = {
        "instance_type": "omics.c.4xlarge",
        "cpu_hours": 0.0,
        "memory_gb_hours": 0.0,
        "storage_gb_hours": 10.0,
    }
    result = cost_report.compute_task_cost(record, _PRICING)
    assert result == pytest.approx(0.000137 * 10.0)


def test_compute_task_cost_unknown_instance_raises() -> None:
    record = {
        "instance_type": "omics.zzz.99xlarge",
        "cpu_hours": 1.0,
        "memory_gb_hours": 1.0,
        "storage_gb_hours": 1.0,
    }
    with pytest.raises(KeyError, match="omics.zzz.99xlarge"):
        cost_report.compute_task_cost(record, _PRICING)


def test_build_cost_report_structure() -> None:
    """The assembled Cost_Report carries pricing_source, per-task entries
    with estimated_usd, and the total."""
    records = [
        {
            "task_name": "hifiasm",
            "instance_type": "omics.c.4xlarge",
            "cpu_hours": 1.0,
            "memory_gb_hours": 2.0,
            "storage_gb_hours": 5.0,
            "storage_type": "DYNAMIC",
        },
        {
            "task_name": "pav",
            "instance_type": "omics.m.2xlarge",
            "cpu_hours": 3.0,
            "memory_gb_hours": 1.0,
            "storage_gb_hours": 10.0,
            "storage_type": "STATIC",
        },
    ]
    report = cost_report.build_cost_report(records, _PRICING)
    assert report["pricing_source"].startswith("healthomics-ap-southeast-1.json@")
    assert report["pricing_source"].endswith(_PRICING["pricing_source_sha256"])
    assert len(report["tasks"]) == 2
    for entry in report["tasks"]:
        assert "estimated_usd" in entry
    expected_total = sum(t["estimated_usd"] for t in report["tasks"])
    assert report["total_estimated_usd"] == pytest.approx(expected_total)
