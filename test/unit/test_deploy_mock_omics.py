"""Mock-omics deploy test for ``scripts/deploy.py`` (Task 17.3).

**Validates: Requirements 13.1, 13.2, 17.13**

This test initially targeted ``moto``'s ``mock_aws`` decorator but, as of
``moto`` 5.1.x, the ``omics`` service returns ``404 Not yet implemented``
for ``ListWorkflows`` / ``CreateWorkflow`` / ``UpdateWorkflow``. The
guidance in the task file is to fall back to ``unittest.mock.patch`` on
``boto3.client`` — that's what this module does. The intent (no real
AWS calls, assert exact number of invocations with exact keyword
arguments) is preserved.

The four cases covered:

* ``test_create_workflow_called_once`` — with an empty ``list_workflows``
  response, ``omics.create_workflow`` is called exactly once with a
  definition zip and a non-empty parameter template.
* ``test_update_when_force`` — with ``list_workflows`` returning a
  matching name, ``--force`` causes exactly one ``update_workflow`` call
  and zero ``create_workflow`` calls.
* ``test_skip_without_force`` — with ``list_workflows`` returning a
  matching name, omitting ``--force`` causes zero calls to both
  ``create_workflow`` and ``update_workflow``.
* ``test_with_budget_alarm`` — passing ``--with-budget-alarm`` together
  with the required threshold and SNS arn triggers exactly one
  ``aws cloudformation deploy`` subprocess call.

``validate_wdl`` is patched out in every test so ``miniwdl check`` does
not need to shell out.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "deploy.py"
)


def _load_deploy_module():
    """Load ``scripts/deploy.py`` as a module named ``deploy``."""
    if "deploy" in sys.modules:
        return sys.modules["deploy"]
    spec = importlib.util.spec_from_file_location("deploy", str(_SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy"] = module
    spec.loader.exec_module(module)
    return module


deploy = _load_deploy_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePaginator:
    """Mimic boto3's paginator API: ``.paginate()`` returns an iterable
    of pages, each page a dict with an ``items`` list."""

    def __init__(self, pages: list[dict]):
        self._pages = pages

    def paginate(self, **_kwargs):
        return iter(self._pages)


def _make_fake_omics(list_pages: list[dict]):
    """Build a MagicMock that quacks like a boto3 omics client."""
    client = mock.MagicMock(name="omics")
    client.get_paginator.return_value = _FakePaginator(list_pages)
    client.create_workflow.return_value = {"id": "wfl-NEW1234", "status": "ACTIVE"}
    client.update_workflow.return_value = {"id": "wfl-EXISTING"}
    return client


def _boto3_factory(omics_client):
    """Return a replacement for ``boto3.client`` that returns
    ``omics_client`` only for the ``omics`` service."""

    def factory(service_name, *args, **kwargs):
        if service_name == "omics":
            return omics_client
        return mock.MagicMock(name=f"{service_name}-client")

    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@mock.patch.object(deploy, "validate_wdl", lambda p: None)
def test_create_workflow_called_once():
    """Empty list_workflows -> exactly one create_workflow call with the
    zip bundle and parameter template attached."""
    omics = _make_fake_omics(list_pages=[{"items": []}])
    with mock.patch("boto3.client", _boto3_factory(omics)):
        rc = deploy.main(
            [
                "--name",
                "test-wf",
                "--region",
                "ap-southeast-1",
            ]
        )

    assert rc == 0, f"deploy.main returned non-zero: {rc}"
    assert omics.create_workflow.call_count == 1, (
        f"expected exactly one create_workflow call, got "
        f"{omics.create_workflow.call_count}"
    )
    assert omics.update_workflow.call_count == 0

    kwargs = omics.create_workflow.call_args.kwargs
    assert kwargs["name"] == "test-wf"
    # definitionZip is a non-empty bytes object that is a valid zip.
    zip_bytes = kwargs["definitionZip"]
    assert isinstance(zip_bytes, bytes) and zip_bytes, (
        "definitionZip must be non-empty bytes"
    )
    with zipfile.ZipFile(BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()
    assert "main.wdl" in names, (
        f"workflow zip missing main.wdl; contents: {names}"
    )
    # parameterTemplate is the parsed JSON (dict) and non-empty.
    tmpl = kwargs["parameterTemplate"]
    assert isinstance(tmpl, dict) and tmpl, "parameterTemplate must be non-empty dict"
    assert "sample_id" in tmpl, "parameter template missing sample_id key"
    assert kwargs.get("main") == "main.wdl"


@mock.patch.object(deploy, "validate_wdl", lambda p: None)
def test_update_when_force():
    """Matching name in list_workflows + --force -> one update_workflow,
    zero create_workflow."""
    existing = {"id": "wfl-EXISTING", "name": "test-wf"}
    omics = _make_fake_omics(list_pages=[{"items": [existing]}])
    with mock.patch("boto3.client", _boto3_factory(omics)):
        rc = deploy.main(
            [
                "--name",
                "test-wf",
                "--region",
                "ap-southeast-1",
                "--force",
            ]
        )

    assert rc == 0
    assert omics.update_workflow.call_count == 1, (
        f"expected exactly one update_workflow call, got "
        f"{omics.update_workflow.call_count}"
    )
    assert omics.create_workflow.call_count == 0

    kwargs = omics.update_workflow.call_args.kwargs
    assert kwargs["id"] == "wfl-EXISTING"
    assert kwargs["name"] == "test-wf"
    assert isinstance(kwargs["definitionZip"], bytes) and kwargs["definitionZip"]
    assert isinstance(kwargs["parameterTemplate"], dict) and kwargs["parameterTemplate"]


@mock.patch.object(deploy, "validate_wdl", lambda p: None)
def test_skip_without_force():
    """Matching name in list_workflows without --force -> neither
    create nor update is called; return code is 0."""
    existing = {"id": "wfl-EXISTING", "name": "test-wf"}
    omics = _make_fake_omics(list_pages=[{"items": [existing]}])
    with mock.patch("boto3.client", _boto3_factory(omics)):
        rc = deploy.main(
            [
                "--name",
                "test-wf",
                "--region",
                "ap-southeast-1",
            ]
        )

    assert rc == 0
    assert omics.create_workflow.call_count == 0
    assert omics.update_workflow.call_count == 0


@mock.patch.object(deploy, "validate_wdl", lambda p: None)
def test_with_budget_alarm():
    """--with-budget-alarm triggers exactly one
    ``aws cloudformation deploy`` subprocess call after the workflow is
    created."""
    omics = _make_fake_omics(list_pages=[{"items": []}])
    sns_topic_arn = "arn:aws:sns:ap-southeast-1:687677765589:alerts"

    with mock.patch("boto3.client", _boto3_factory(omics)), mock.patch.object(
        deploy, "subprocess"
    ) as subprocess_mock:
        subprocess_mock.run.return_value = mock.MagicMock(returncode=0)
        # Make subprocess.CalledProcessError still behave like the real
        # exception so deploy.py's except-clauses stay valid.
        subprocess_mock.CalledProcessError = Exception

        rc = deploy.main(
            [
                "--name",
                "test-wf",
                "--region",
                "ap-southeast-1",
                "--with-budget-alarm",
                "--budget-threshold-usd",
                "100",
                "--budget-sns-topic-arn",
                sns_topic_arn,
            ]
        )

    assert rc == 0
    assert omics.create_workflow.call_count == 1

    # Exactly one subprocess.run call, and it is the cloudformation deploy.
    assert subprocess_mock.run.call_count == 1, (
        f"expected exactly one subprocess.run call, got "
        f"{subprocess_mock.run.call_count}"
    )
    cmd = subprocess_mock.run.call_args.args[0]
    assert cmd[:3] == ["aws", "cloudformation", "deploy"], (
        f"subprocess call was not aws cloudformation deploy: {cmd}"
    )
    # Template file is scripts/budget-alarm.yaml.
    assert "--template-file" in cmd
    template_arg = cmd[cmd.index("--template-file") + 1]
    assert template_arg.endswith("budget-alarm.yaml"), (
        f"unexpected template file: {template_arg!r}"
    )
    # Region, threshold and SNS ARN plumbed through.
    assert "--region" in cmd
    assert cmd[cmd.index("--region") + 1] == "ap-southeast-1"
    overrides_idx = cmd.index("--parameter-overrides")
    overrides = cmd[overrides_idx + 1 : overrides_idx + 3]
    assert "Threshold=100.0" in overrides or "Threshold=100" in overrides, (
        f"threshold not in overrides: {overrides}"
    )
    assert f"SnsTopicArn={sns_topic_arn}" in overrides, (
        f"SNS topic ARN not in overrides: {overrides}"
    )


@mock.patch.object(deploy, "validate_wdl", lambda p: None)
def test_with_budget_alarm_requires_threshold_and_sns():
    """``--with-budget-alarm`` without the companion flags exits non-zero
    *without* attempting any subprocess call."""
    omics = _make_fake_omics(list_pages=[{"items": []}])
    with mock.patch("boto3.client", _boto3_factory(omics)), mock.patch.object(
        deploy, "subprocess"
    ) as subprocess_mock:
        subprocess_mock.run.return_value = mock.MagicMock(returncode=0)
        subprocess_mock.CalledProcessError = Exception
        rc = deploy.main(
            [
                "--name",
                "test-wf",
                "--region",
                "ap-southeast-1",
                "--with-budget-alarm",
            ]
        )

    assert rc != 0, "deploy.main should reject --with-budget-alarm without threshold/arn"
    assert subprocess_mock.run.call_count == 0, (
        "cloudformation deploy should not run when required args are missing"
    )
