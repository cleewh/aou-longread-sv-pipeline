# Feature: aou-longread-sv-pipeline, Property 8: Rendered IAM policy is least-privilege
"""Property-based tests for :func:`iam.render.render` (Task 13.4).

**Validates: Requirements 9.1, 9.3**

Property 8: *for any values substituted into
``iam/execution_role_policy.json.tmpl`` for ``INPUT_BUCKET``,
``OUTPUT_BUCKET``, ``OUTPUT_PREFIX``, ``ECR_REPO_ARNS``, and
``ACCOUNT_ID``, the resulting policy document SHALL parse as valid JSON
AND SHALL NOT contain any ``Action`` matching ``s3:*`` or
``s3:ListAllMyBuckets`` AND every ``s3:GetObject`` Resource SHALL
reference only ``INPUT_BUCKET`` AND every ``s3:PutObject`` Resource SHALL
reference only the rendered ``OUTPUT_BUCKET/OUTPUT_PREFIX`` scope AND
every ``ecr:BatchGetImage`` and ``ecr:GetDownloadUrlForLayer`` Resource
SHALL reference only ARNs listed in ``ECR_REPO_ARNS``.*

The render function is pure (file I/O on the template + string substitution
+ ``json.loads``), so Hypothesis exhausts the substitution space directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st


# ---------------------------------------------------------------------------
# Import iam/render.py by path (``iam`` is not a Python package).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RENDER_PATH = _REPO_ROOT / "iam" / "render.py"
_TEMPLATE_PATH = _REPO_ROOT / "iam" / "execution_role_policy.json.tmpl"

_spec = importlib.util.spec_from_file_location("_aou_iam_render", _RENDER_PATH)
assert _spec is not None and _spec.loader is not None
_render_mod = importlib.util.module_from_spec(_spec)
sys.modules["_aou_iam_render"] = _render_mod
_spec.loader.exec_module(_render_mod)

render = _render_mod.render


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


# S3 bucket naming rules (simplified): 3..63 chars, lowercase alphanumeric or
# hyphens, start/end alphanumeric, no consecutive dots. The render function
# is character-agnostic, so we keep the alphabet tight to steer clear of
# characters that are meaningful inside JSON (", \, {, }, etc).
_BUCKET_ALPHA = "abcdefghijklmnopqrstuvwxyz0123456789-"
bucket_name = st.text(
    alphabet=_BUCKET_ALPHA, min_size=3, max_size=24
).filter(lambda s: s[0].isalnum() and s[-1].isalnum())

# Output prefix — path-like, trailing slash, printable ASCII without JSON
# breakers. Allowed chars mirror S3 key rules the pipeline uses in practice.
_PREFIX_ALPHA = "abcdefghijklmnopqrstuvwxyz0123456789-_/."
output_prefix = st.text(
    alphabet=_PREFIX_ALPHA, min_size=1, max_size=40
).map(lambda s: s.rstrip("/") + "/")

# 12-digit AWS account ids.
account_id = st.from_regex(r"\A\d{12}\Z", fullmatch=True)

# ECR repo ARNs matching ``arn:aws:ecr:ap-southeast-1:\d{12}:repository/aou-sv/[a-z0-9_-]+``.
_REPO_SUFFIX_ALPHA = "abcdefghijklmnopqrstuvwxyz0123456789_-"


@st.composite
def ecr_repo_arn(draw):
    acct = draw(st.from_regex(r"\A\d{12}\Z", fullmatch=True))
    suffix = draw(
        st.text(alphabet=_REPO_SUFFIX_ALPHA, min_size=1, max_size=24)
    )
    return f"arn:aws:ecr:ap-southeast-1:{acct}:repository/aou-sv/{suffix}"


ecr_repo_arns = st.lists(ecr_repo_arn(), min_size=1, max_size=10, unique=True)


# ---------------------------------------------------------------------------
# Small helpers for statement introspection
# ---------------------------------------------------------------------------


def _actions(stmt: dict) -> list[str]:
    raw = stmt.get("Action", [])
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def _resources(stmt: dict) -> list[str]:
    raw = stmt.get("Resource", [])
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def _statements_containing(policy: dict, action_needle: str) -> list[dict]:
    out: list[dict] = []
    for stmt in policy.get("Statement", []):
        if action_needle in _actions(stmt):
            out.append(stmt)
    return out


# ---------------------------------------------------------------------------
# Property 8 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@given(
    input_bucket=bucket_name,
    output_bucket=bucket_name,
    output_prefix=output_prefix,
    account_id=account_id,
    ecr_repo_arns=ecr_repo_arns,
)
@settings(max_examples=100)
def test_rendered_policy_parses_as_json(
    input_bucket, output_bucket, output_prefix, account_id, ecr_repo_arns
):
    """The rendered document must be a dict with a non-empty Statement list."""
    policy = render(
        template_path=_TEMPLATE_PATH,
        input_bucket=input_bucket,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        ecr_repo_arns=ecr_repo_arns,
        account_id=account_id,
    )
    assert isinstance(policy, dict)
    assert policy.get("Version") == "2012-10-17"
    assert isinstance(policy.get("Statement"), list)
    assert len(policy["Statement"]) > 0


@pytest.mark.property_test
@given(
    input_bucket=bucket_name,
    output_bucket=bucket_name,
    output_prefix=output_prefix,
    account_id=account_id,
    ecr_repo_arns=ecr_repo_arns,
)
@settings(max_examples=100)
def test_no_s3_wildcard_actions(
    input_bucket, output_bucket, output_prefix, account_id, ecr_repo_arns
):
    """No statement may contain ``s3:*`` or ``s3:ListAllMyBuckets``."""
    policy = render(
        template_path=_TEMPLATE_PATH,
        input_bucket=input_bucket,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        ecr_repo_arns=ecr_repo_arns,
        account_id=account_id,
    )
    for stmt in policy["Statement"]:
        actions = _actions(stmt)
        assert "s3:*" not in actions, f"s3:* present in {stmt!r}"
        assert "s3:ListAllMyBuckets" not in actions, (
            f"s3:ListAllMyBuckets present in {stmt!r}"
        )


@pytest.mark.property_test
@given(
    input_bucket=bucket_name,
    output_bucket=bucket_name,
    output_prefix=output_prefix,
    account_id=account_id,
    ecr_repo_arns=ecr_repo_arns,
)
@settings(max_examples=100)
def test_s3_get_object_scoped_to_input_bucket(
    input_bucket, output_bucket, output_prefix, account_id, ecr_repo_arns
):
    """Every ``s3:GetObject`` Resource must reference only the input bucket."""
    policy = render(
        template_path=_TEMPLATE_PATH,
        input_bucket=input_bucket,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        ecr_repo_arns=ecr_repo_arns,
        account_id=account_id,
    )
    expected = f"arn:aws:s3:::{input_bucket}/*"
    for stmt in _statements_containing(policy, "s3:GetObject"):
        for resource in _resources(stmt):
            assert resource == expected, (
                f"s3:GetObject Resource {resource!r} not scoped to INPUT_BUCKET "
                f"(expected {expected!r})"
            )


@pytest.mark.property_test
@given(
    input_bucket=bucket_name,
    output_bucket=bucket_name,
    output_prefix=output_prefix,
    account_id=account_id,
    ecr_repo_arns=ecr_repo_arns,
)
@settings(max_examples=100)
def test_s3_put_object_scoped_to_output_prefix(
    input_bucket, output_bucket, output_prefix, account_id, ecr_repo_arns
):
    """Every ``s3:PutObject`` Resource must reference only ``OUTPUT_BUCKET/OUTPUT_PREFIX*``."""
    policy = render(
        template_path=_TEMPLATE_PATH,
        input_bucket=input_bucket,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        ecr_repo_arns=ecr_repo_arns,
        account_id=account_id,
    )
    expected = f"arn:aws:s3:::{output_bucket}/{output_prefix}*"
    for stmt in _statements_containing(policy, "s3:PutObject"):
        for resource in _resources(stmt):
            assert resource == expected, (
                f"s3:PutObject Resource {resource!r} not scoped to "
                f"OUTPUT_BUCKET/OUTPUT_PREFIX (expected {expected!r})"
            )


@pytest.mark.property_test
@given(
    input_bucket=bucket_name,
    output_bucket=bucket_name,
    output_prefix=output_prefix,
    account_id=account_id,
    ecr_repo_arns=ecr_repo_arns,
)
@settings(max_examples=100)
def test_ecr_resources_equal_supplied_arns(
    input_bucket, output_bucket, output_prefix, account_id, ecr_repo_arns
):
    """Every ``ecr:BatchGetImage`` / ``ecr:GetDownloadUrlForLayer`` Resource must be in the supplied ARN list."""
    policy = render(
        template_path=_TEMPLATE_PATH,
        input_bucket=input_bucket,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        ecr_repo_arns=ecr_repo_arns,
        account_id=account_id,
    )
    expected_set = set(ecr_repo_arns)
    for needle in ("ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"):
        for stmt in _statements_containing(policy, needle):
            observed = set(_resources(stmt))
            assert observed == expected_set, (
                f"{needle} Resource set {observed!r} != supplied ECR ARN set "
                f"{expected_set!r}"
            )
