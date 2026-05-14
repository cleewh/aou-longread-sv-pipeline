"""Unit tests for :mod:`writer` (Task 3.8).

Complements the Hypothesis property test in
``test/property/test_writer_property.py`` with concrete examples for
the schema loader, field construction, and validation failure paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

import writer  # noqa: E402 — sys.path patched by test/conftest.py


def _sample_manifest_path(tmp_path: Path) -> Path:
    manifest = {
        "sample_id": "HG002",
        "hifi_reads_bam": "s3://bucket/hg002.bam",
        "reference_fasta": "s3://bucket/ref.fa",
        "reference_fai": "s3://bucket/ref.fa.fai",
        "output_prefix": "s3://bucket/outputs/HG002/",
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


_PRICING = {
    "pricing_source_sha256": "a" * 64,
    "instance_usd_per_hour": {"omics.c.4xlarge": 1.36},
    "run_storage_usd_per_gb_hour": {"DYNAMIC": 0.000137, "STATIC": 0.000206},
}


_TOOL_INFO = {
    tool: {"version": "1.0.0", "image_digest": "sha256:" + "0" * 64}
    for tool in (
        "hifiasm",
        "pav",
        "pav2svs",
        "sniffles2",
        "pbsv",
        "pbmm2",
        "harmoniser",
    )
}

_PER_CALLER_STATUS = {
    "hifiasm_pav": "succeeded",
    "sniffles2": "succeeded",
    "pbsv": "succeeded",
    "harmoniser": "succeeded",
}

_OUTPUTS = {
    "harmonised_sv_vcf": "s3://bucket/outputs/HG002/HG002.sv.harmonised.vcf.gz",
    "pav_sv_vcf": "s3://bucket/outputs/HG002/HG002.sv.pav.vcf.gz",
    "sniffles2_sv_vcf": None,
    "pbsv_sv_vcf": None,
    "hifiasm_hap1_fasta": None,
    "hifiasm_hap2_fasta": None,
}


def _base_kwargs(manifest_path: Path) -> dict:
    return dict(
        pipeline_version="0.1.0",
        git_commit="abc1234",
        input_manifest_path=manifest_path,
        region="ap-southeast-1",
        healthomics_run_id="1234567",
        workflow_id="wfl-abc123",
        workflow_name="aou-longread-sv-pipeline",
        workflow_version="0.1.0",
        start_time="2025-01-15T03:14:00Z",
        end_time="2025-01-15T07:42:11Z",
        status="COMPLETED",
        storage_type="DYNAMIC",
        per_caller_status=_PER_CALLER_STATUS,
        tool_info=_TOOL_INFO,
        outputs=_OUTPUTS,
        cost_records=[],
        pricing=_PRICING,
    )


def test_build_run_metadata_required_top_level_keys(tmp_path: Path) -> None:
    metadata = writer.build_run_metadata(**_base_kwargs(_sample_manifest_path(tmp_path)))
    for key in (
        "pipeline",
        "run",
        "tools",
        "per_caller_status",
        "outputs",
        "Cost_Report",
        "input_manifest",
    ):
        assert key in metadata, f"missing top-level key: {key}"
    # SHA-256 is hex, 64 chars.
    assert len(metadata["pipeline"]["input_manifest_sha256"]) == 64


def test_build_run_metadata_echoes_input_manifest(tmp_path: Path) -> None:
    manifest_path = _sample_manifest_path(tmp_path)
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = writer.build_run_metadata(**_base_kwargs(manifest_path))
    assert metadata["input_manifest"] == original


def test_validate_schema_accepts_well_formed(tmp_path: Path) -> None:
    metadata = writer.build_run_metadata(**_base_kwargs(_sample_manifest_path(tmp_path)))
    writer.validate_schema(metadata)  # no raise


def test_validate_schema_rejects_missing_region(tmp_path: Path) -> None:
    metadata = writer.build_run_metadata(**_base_kwargs(_sample_manifest_path(tmp_path)))
    del metadata["pipeline"]["region"]
    with pytest.raises(jsonschema.ValidationError):
        writer.validate_schema(metadata)


def test_validate_schema_rejects_bad_git_commit(tmp_path: Path) -> None:
    metadata = writer.build_run_metadata(**_base_kwargs(_sample_manifest_path(tmp_path)))
    metadata["pipeline"]["git_commit"] = "ZZZZZZZZ"  # non-hex
    with pytest.raises(jsonschema.ValidationError):
        writer.validate_schema(metadata)


def test_validate_schema_rejects_bad_caller_status(tmp_path: Path) -> None:
    metadata = writer.build_run_metadata(**_base_kwargs(_sample_manifest_path(tmp_path)))
    metadata["per_caller_status"]["sniffles2"] = "maybe"
    with pytest.raises(jsonschema.ValidationError):
        writer.validate_schema(metadata)


def test_validate_schema_rejects_bad_image_digest(tmp_path: Path) -> None:
    metadata = writer.build_run_metadata(**_base_kwargs(_sample_manifest_path(tmp_path)))
    metadata["tools"]["hifiasm"]["image_digest"] = "not-a-digest"
    with pytest.raises(jsonschema.ValidationError):
        writer.validate_schema(metadata)
