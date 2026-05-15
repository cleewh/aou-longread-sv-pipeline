# Feature: aou-longread-sv-pipeline, Property 9: Region-residency gate enforces ap-southeast-1
"""Property-based tests for :mod:`submit_run.residency` (Task 10.2).

**Validates: Requirements 10.2, 17.7 (S3 portion)**

Property 9: *for any Input_Manifest and any mapping of S3 buckets to region
names, the submit-run.py region-residency gate SHALL return success if and
only if every bucket referenced in the manifest reports its region as
ap-southeast-1. When the gate fails, the returned error message SHALL name at
least one offending bucket and its reported region.*

These tests also exercise the companion ECR residency gate (same module,
same Task 10.1) because both gates share the "refuse anything not in
ap-southeast-1" invariant.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings, strategies as st

from submit_run.residency import (  # noqa: E402 — sys.path patched by test/conftest.py
    _build_ecr_regex,

    EcrResidencyError,
    RegionResidencyError,
    check_ecr_residency,
    check_region_residency,
)

TEST_REGION = "ap-southeast-1"  # Test fixture region


# ---------------------------------------------------------------------------
# Fake S3 client — only the ``get_bucket_location`` method is ever called.
# ---------------------------------------------------------------------------


class FakeS3Client:
    """Map bucket names to LocationConstraint values for tests."""

    def __init__(self, bucket_to_region: dict[str, str | None]):
        self._map = bucket_to_region

    def get_bucket_location(self, *, Bucket: str) -> dict:  # noqa: N803 - boto3 API shape
        if Bucket not in self._map:
            raise KeyError(f"FakeS3Client has no mapping for bucket {Bucket!r}")
        return {"LocationConstraint": self._map[Bucket]}


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


_BUCKET_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"

bucket_name = st.text(
    alphabet=_BUCKET_NAME_ALPHABET, min_size=3, max_size=24
).filter(lambda s: s[0].isalnum() and s[-1].isalnum())

# Values that GetBucketLocation can legitimately return. ``None`` represents
# a legacy us-east-1 bucket (API-historical quirk).
_REGION_VALUES = ["ap-southeast-1", "us-east-1", "eu-west-1", "ap-northeast-1", None]
region_value = st.sampled_from(_REGION_VALUES)


# S3 URI fields the residency gate scans (subset that matches _S3_URI_FIELDS
# in the implementation). Using a subset keeps examples small while still
# exercising all code paths.
_S3_FIELDS = [
    "hifi_reads_bam",
    "hifi_reads_bai",
    "reference_fasta",
    "reference_fai",
    "output_prefix",
    "input_manifest_json",
    "harmoniser_filter_override_json",
]

manifest_fields_strategy = st.lists(
    st.sampled_from(_S3_FIELDS), min_size=1, max_size=len(_S3_FIELDS), unique=True
)


@st.composite
def manifest_and_bucket_map(draw):
    """Build an (Input_Manifest, bucket->region map) pair.

    Every field drawn gets a unique bucket name mapped to a randomly chosen
    region. The returned manifest is the minimal dict the gate cares about —
    other fields would be ignored. Using fresh bucket names per field avoids
    the case where the same bucket is referenced by two fields (which is
    valid but would collapse the "offending bucket" identification to a
    single bucket).
    """
    fields = draw(manifest_fields_strategy)
    buckets = draw(
        st.lists(bucket_name, min_size=len(fields), max_size=len(fields), unique=True)
    )
    regions = [draw(region_value) for _ in fields]
    manifest = {
        field: f"s3://{bucket}/some/key.bin" if not field.endswith("prefix") else f"s3://{bucket}/out/"
        for field, bucket in zip(fields, buckets)
    }
    bucket_map = dict(zip(buckets, regions))
    return manifest, bucket_map


# ---------------------------------------------------------------------------
# Region residency tests — Property 9
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@given(manifest_fields_strategy, st.lists(bucket_name, min_size=1, max_size=7, unique=True))
@settings(max_examples=100)
def test_all_in_region_passes(fields, buckets):
    """When every referenced bucket maps to ap-southeast-1, the gate returns None."""
    fields = fields[: len(buckets)]
    buckets = buckets[: len(fields)]
    assume(len(fields) == len(buckets) and len(fields) >= 1)
    manifest = {
        field: (
            f"s3://{bucket}/out/"
            if field.endswith("prefix")
            else f"s3://{bucket}/x.bin"
        )
        for field, bucket in zip(fields, buckets)
    }
    client = FakeS3Client({b: TEST_REGION for b in buckets})
    # Should not raise.
    assert check_region_residency(manifest, client, TEST_REGION) is None


@pytest.mark.property_test
@given(manifest_and_bucket_map())
@settings(max_examples=100)
def test_offending_bucket_named_in_error(data):
    """When any bucket is out of region, the error names a bucket+region mismatch."""
    manifest, bucket_map = data
    has_offender = any(
        region != TEST_REGION for region in bucket_map.values()
    )
    client = FakeS3Client(bucket_map)
    if not has_offender:
        assert check_region_residency(manifest, client, TEST_REGION) is None
        return
    with pytest.raises(RegionResidencyError) as excinfo:
        check_region_residency(manifest, client, TEST_REGION)
    message = str(excinfo.value)
    # At least one offending bucket AND its reported region must appear in
    # the message (Property 9 "name at least one offending bucket").
    offenders = [(b, r) for b, r in bucket_map.items() if r != TEST_REGION]
    assert any(
        b in message and (r or "us-east-1") in message for (b, r) in offenders
    ), (
        f"Error message did not name any offending (bucket, region) pair. "
        f"offenders={offenders!r}, message={message!r}"
    )


# ---------------------------------------------------------------------------
# ECR residency tests — companion to Property 9
# ---------------------------------------------------------------------------


# Strategy for a valid ap-southeast-1 ECR URI.
_ACCOUNT = st.from_regex(r"\A\d{12}\Z", fullmatch=True)
_REPO = st.from_regex(r"\A[a-z0-9][a-z0-9/_.-]{0,40}[a-z0-9]\Z", fullmatch=True)
_SHA256 = st.from_regex(r"\A[0-9a-f]{64}\Z", fullmatch=True)


@st.composite
def good_ecr_uri(draw):
    account = draw(_ACCOUNT)
    repo = draw(_REPO)
    digest = draw(_SHA256)
    return f"{account}.dkr.ecr.ap-southeast-1.amazonaws.com/{repo}@sha256:{digest}"


_BAD_REGIONS = st.sampled_from(
    ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "ap-southeast-2"]
)


@st.composite
def bad_ecr_uri(draw):
    account = draw(_ACCOUNT)
    repo = draw(_REPO)
    digest = draw(_SHA256)
    region = draw(_BAD_REGIONS)
    return f"{account}.dkr.ecr.{region}.amazonaws.com/{repo}@sha256:{digest}"


@pytest.mark.property_test
@given(st.lists(good_ecr_uri(), min_size=1, max_size=10))
@settings(max_examples=100)
def test_check_ecr_residency_accepts_ap_southeast_1(uris):
    """URIs that match the ap-southeast-1 ECR shape are accepted."""
    # Every URI should match the implementation regex first (sanity).
    assert all(_build_ecr_regex(TEST_REGION).match(u) for u in uris)
    assert check_ecr_residency(uris, TEST_REGION) is None


@pytest.mark.property_test
@given(
    st.lists(good_ecr_uri(), min_size=0, max_size=5),
    bad_ecr_uri(),
    st.lists(good_ecr_uri(), min_size=0, max_size=5),
)
@settings(max_examples=100)
def test_check_ecr_residency_rejects_other_regions(pre, bad, post):
    """A URI in any other region anywhere in the list triggers rejection."""
    uris = [*pre, bad, *post]
    with pytest.raises(EcrResidencyError) as excinfo:
        check_ecr_residency(uris, TEST_REGION)
    assert bad in str(excinfo.value)
