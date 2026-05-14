# Feature: aou-longread-sv-pipeline, Property 5: run_metadata.json conforms to schema
"""Property-based tests for :mod:`writer` (Task 3.9).

**Validates: Requirements 7.2, 12.2, 16.2**

Property 5 states that any ``run_metadata.json`` produced by
:func:`writer.build_run_metadata` parses as JSON and passes every
required field defined in ``run_metadata.schema.json``. The strategies
below generate arbitrary combinations of per-caller statuses, tool
versions, image digests, HealthOmics run identifiers, and Cost_Report
values; each iteration round-trips through the writer and the
:mod:`jsonschema` validator.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

import cost_report  # noqa: E402 — sys.path patched by test/conftest.py
import writer  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_SEMVER = st.from_regex(r"^[0-9]+\.[0-9]+\.[0-9]+$", fullmatch=True)
_GIT_COMMIT = st.one_of(
    st.from_regex(r"^[0-9a-f]{7,40}$", fullmatch=True),
    st.just("unknown"),
)
_SHA256_HEX = st.from_regex(r"^[0-9a-f]{64}$", fullmatch=True)
_IMAGE_DIGEST = _SHA256_HEX.map(lambda s: f"sha256:{s}")

_CALLER_KEYS = ("hifiasm_pav", "sniffles2", "pbsv", "harmoniser")
_TOOL_KEYS = (
    "hifiasm",
    "pav",
    "pav2svs",
    "sniffles2",
    "pbsv",
    "pbmm2",
    "harmoniser",
)
_STATUS_VALUES = ("succeeded", "skipped", "failed")

_per_caller_status = st.fixed_dictionaries(
    {key: st.sampled_from(_STATUS_VALUES) for key in _CALLER_KEYS}
)

_tool_info = st.fixed_dictionaries(
    {
        tool: st.fixed_dictionaries(
            {
                "version": st.text(
                    alphabet=st.characters(
                        whitelist_categories=("Ll", "Lu", "Nd"),
                        whitelist_characters=".-_",
                    ),
                    min_size=1,
                    max_size=20,
                ),
                "image_digest": _IMAGE_DIGEST,
            }
        )
        for tool in _TOOL_KEYS
    }
)

_INSTANCE_NAME = "omics.c.4xlarge"

_hours = st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False)

_cost_records = st.lists(
    st.fixed_dictionaries(
        {
            "task_name": st.sampled_from(list(_TOOL_KEYS)),
            "instance_type": st.just(_INSTANCE_NAME),
            "cpu_hours": _hours,
            "memory_gb_hours": _hours,
            "storage_gb_hours": _hours,
            "storage_type": st.sampled_from(["DYNAMIC", "STATIC"]),
        }
    ),
    min_size=0,
    max_size=5,
)


_SYNTHETIC_PRICING = {
    "pricing_source_sha256": "0" * 64,
    "instance_usd_per_hour": {_INSTANCE_NAME: 1.36},
    "run_storage_usd_per_gb_hour": {"DYNAMIC": 0.000137, "STATIC": 0.000206},
}


_s3_uri = st.from_regex(
    r"^s3://[a-z0-9][a-z0-9\-]{1,40}/[a-z0-9_\-/]{1,40}\.(vcf\.gz|fa\.gz)$",
    fullmatch=True,
)

_outputs = st.fixed_dictionaries(
    mapping={"harmonised_sv_vcf": _s3_uri},
    optional={
        "pav_sv_vcf": st.one_of(_s3_uri, st.none()),
        "sniffles2_sv_vcf": st.one_of(_s3_uri, st.none()),
        "pbsv_sv_vcf": st.one_of(_s3_uri, st.none()),
        "hifiasm_hap1_fasta": st.one_of(_s3_uri, st.none()),
        "hifiasm_hap2_fasta": st.one_of(_s3_uri, st.none()),
    },
)


_ISO_TIME = st.from_regex(
    r"^20[0-9]{2}-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]Z$",
    fullmatch=True,
)


# ---------------------------------------------------------------------------
# Property 5 test
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@settings(max_examples=100, deadline=None)
@given(
    pipeline_version=_SEMVER,
    git_commit=_GIT_COMMIT,
    healthomics_run_id=st.from_regex(r"^[0-9]{5,10}$", fullmatch=True),
    workflow_id=st.from_regex(r"^wfl-[0-9a-f]{6,10}$", fullmatch=True),
    workflow_name=st.sampled_from(["aou-longread-sv-pipeline"]),
    workflow_version=_SEMVER,
    start_time=_ISO_TIME,
    end_time=_ISO_TIME,
    status=st.sampled_from(["COMPLETED", "FAILED", "CANCELLED"]),
    storage_type=st.sampled_from(["DYNAMIC", "STATIC"]),
    per_caller_status=_per_caller_status,
    tool_info=_tool_info,
    outputs=_outputs,
    cost_records=_cost_records,
)
def test_writer_output_conforms_to_schema(
    pipeline_version: str,
    git_commit: str,
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
    cost_records: list,
) -> None:
    """Full run_metadata.json built by the writer validates against the
    bundled schema without raising."""
    # Write a realistic Input_Manifest to disk so build_run_metadata can
    # SHA-256 it and echo it back into the output.
    manifest_body = {
        "sample_id": "HG002",
        "hifi_reads_bam": "s3://bucket/hg002.bam",
        "reference_fasta": "s3://bucket/ref.fa",
        "reference_fai": "s3://bucket/ref.fa.fai",
        "output_prefix": "s3://bucket/outputs/HG002/",
    }
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_body), encoding="utf-8")

        metadata = writer.build_run_metadata(
            pipeline_version=pipeline_version,
            git_commit=git_commit,
            input_manifest_path=manifest_path,
            region="ap-southeast-1",
            healthomics_run_id=healthomics_run_id,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            workflow_version=workflow_version,
            start_time=start_time,
            end_time=end_time,
            status=status,
            storage_type=storage_type,
            per_caller_status=per_caller_status,
            tool_info=tool_info,
            outputs=outputs,
            cost_records=cost_records,
            pricing=_SYNTHETIC_PRICING,
        )

    # Round-trip through JSON to verify it is JSON-serialisable, then
    # validate against the bundled schema.
    reloaded = json.loads(json.dumps(metadata))
    writer.validate_schema(reloaded)
