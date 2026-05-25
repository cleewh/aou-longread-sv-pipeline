"""Submission flag pass-through tests for ``scripts/submit-run.py`` (Task 17.4).

**Validates: Requirements 17.2, 17.12**

These are example-based tests (complementing the property test for
residency, Property 9) that assert the ``run_storage_type`` and
``enable_run_cache`` values supplied on the Input_Manifest reach
``aws omics start-run`` arguments unchanged.

All AWS clients and ``subprocess`` calls are stubbed out via
``unittest.mock.patch``:

* ``boto3.client("s3", ...)``    → MagicMock whose ``get_bucket_location``
  always returns ``LocationConstraint="ap-southeast-1"`` so the region
  residency gate passes for any ``ap-southeast-1`` bucket.
* ``boto3.client("omics", ...)`` → MagicMock whose ``start_run`` records
  the kwargs it was called with and returns a canned ``{"id": ..., "status": ...}``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "submit-run.py"


# ---------------------------------------------------------------------------
# Load ``scripts/submit-run.py`` as a module named ``submit_run_cli``
# (``submit_run`` is already registered as the helper package in conftest.py).
# ---------------------------------------------------------------------------


def _load_submit_run_cli():
    if "submit_run_cli" in sys.modules:
        return sys.modules["submit_run_cli"]
    spec = importlib.util.spec_from_file_location(
        "submit_run_cli", str(_SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["submit_run_cli"] = module
    spec.loader.exec_module(module)
    return module


submit_run_cli = _load_submit_run_cli()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BUCKET = "aou-longread-sv-123456789012-ap-southeast-1"


def _base_manifest() -> dict:
    """Minimal Input_Manifest with all fields the residency gate inspects."""
    return {
        "sample_id": "HG002_chr20",
        "aws_account_id": "123456789012",
        "hifi_reads_bam": f"s3://{_BUCKET}/test/e2e/HG002_chr20.hifi.bam",
        "hifi_reads_bai": f"s3://{_BUCKET}/test/e2e/HG002_chr20.hifi.bam.bai",
        "reference_fasta": f"s3://{_BUCKET}/test/e2e/GRCh38_no_alt.fa",
        "reference_fai": f"s3://{_BUCKET}/test/e2e/GRCh38_no_alt.fa.fai",
        "output_prefix": f"s3://{_BUCKET}/test/e2e/outputs/HG002_chr20/",
        "input_manifest_json": f"s3://{_BUCKET}/test/e2e/inputs.json",
        "run_hifiasm_pav": True,
        "run_sniffles2": True,
        "run_pbsv": True,
        "shard_by_chromosome": True,
        # Default: DYNAMIC + cache enabled; individual tests override.
        "run_storage_type": "DYNAMIC",
        "enable_run_cache": True,
    }


def _write_manifest(tmp_path: Path, overrides: dict | None = None) -> Path:
    manifest = _base_manifest()
    if overrides:
        manifest.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _make_s3_mock() -> mock.MagicMock:
    """Mock S3 client with every bucket in ap-southeast-1."""
    client = mock.MagicMock(name="s3")
    client.get_bucket_location.return_value = {
        "LocationConstraint": "ap-southeast-1"
    }
    return client


def _make_omics_mock() -> mock.MagicMock:
    """Mock omics client whose start_run returns a canned run id."""
    client = mock.MagicMock(name="omics")
    client.start_run.return_value = {"id": "run-fake-12345", "status": "PENDING"}
    return client


def _boto3_factory(s3_mock: mock.MagicMock, omics_mock: mock.MagicMock):
    def factory(service_name, *args, **kwargs):
        if service_name == "s3":
            return s3_mock
        if service_name == "omics":
            return omics_mock
        return mock.MagicMock(name=f"{service_name}-client")

    return factory


def _run_submit(manifest_path: Path) -> tuple[int, mock.MagicMock]:
    s3 = _make_s3_mock()
    omics = _make_omics_mock()
    with mock.patch("boto3.client", _boto3_factory(s3, omics)):
        rc = submit_run_cli.main(
            [
                "--manifest",
                str(manifest_path),
                "--workflow-id",
                "wfl-FAKEID",
                "--role-arn",
                "arn:aws:iam::123456789012:role/fake-omics-exec",
                "--region",
                "ap-southeast-1",
            ]
        )
    return rc, omics


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_storage_type_static_passes_through(tmp_path: Path) -> None:
    """``run_storage_type="STATIC"`` in the manifest reaches
    ``omics.start_run`` as ``storageType="STATIC"`` unchanged."""
    manifest_path = _write_manifest(
        tmp_path, {"run_storage_type": "STATIC", "enable_run_cache": False}
    )
    rc, omics = _run_submit(manifest_path)

    assert rc == 0, f"submit-run returned non-zero: {rc}"
    assert omics.start_run.call_count == 1
    kwargs = omics.start_run.call_args.kwargs
    assert kwargs["storageType"] == "STATIC", (
        f"expected storageType='STATIC', got {kwargs.get('storageType')!r}"
    )


def test_storage_type_dynamic_passes_through(tmp_path: Path) -> None:
    """``run_storage_type="DYNAMIC"`` in the manifest reaches
    ``omics.start_run`` as ``storageType="DYNAMIC"`` unchanged."""
    manifest_path = _write_manifest(
        tmp_path, {"run_storage_type": "DYNAMIC", "enable_run_cache": False}
    )
    rc, omics = _run_submit(manifest_path)

    assert rc == 0
    assert omics.start_run.call_count == 1
    kwargs = omics.start_run.call_args.kwargs
    assert kwargs["storageType"] == "DYNAMIC", (
        f"expected storageType='DYNAMIC', got {kwargs.get('storageType')!r}"
    )


def test_enable_run_cache_true_with_id_sets_cache_kwargs(tmp_path: Path) -> None:
    """``enable_run_cache=True`` + a supplied ``run_cache_id`` in the
    manifest causes ``omics.start_run`` to be called with a matching
    ``cacheId`` kwarg (Req 17.12 / Design D16)."""
    cache_id = "cache-abc123"
    manifest_path = _write_manifest(
        tmp_path,
        {
            "run_storage_type": "DYNAMIC",
            "enable_run_cache": True,
            "run_cache_id": cache_id,
        },
    )
    rc, omics = _run_submit(manifest_path)

    assert rc == 0
    assert omics.start_run.call_count == 1
    kwargs = omics.start_run.call_args.kwargs
    assert kwargs.get("cacheId") == cache_id, (
        f"expected cacheId={cache_id!r}, got {kwargs.get('cacheId')!r}"
    )
    assert "cacheBehavior" in kwargs, (
        "expected cacheBehavior kwarg to accompany cacheId"
    )


def test_enable_run_cache_false_omits_cache_kwargs(tmp_path: Path) -> None:
    """``enable_run_cache=False`` in the manifest means
    ``omics.start_run`` is called WITHOUT a ``cacheId`` kwarg — evidence
    that the pass-through is sensitive to the manifest's opt-in toggle."""
    manifest_path = _write_manifest(
        tmp_path,
        {"run_storage_type": "DYNAMIC", "enable_run_cache": False},
    )
    rc, omics = _run_submit(manifest_path)

    assert rc == 0
    assert omics.start_run.call_count == 1
    kwargs = omics.start_run.call_args.kwargs
    assert "cacheId" not in kwargs, (
        f"expected no cacheId kwarg when enable_run_cache=False, got "
        f"kwargs={list(kwargs)!r}"
    )
    assert "cacheBehavior" not in kwargs
