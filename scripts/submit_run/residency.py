# Task 10.1: Residency checks for submit-run.py.
"""Residency gates for the AoU long-read SV pipeline submission tool.

Requirements: 10.2, 10.3, 10.4, 17.7
Design: D10, submit-run pseudocode, Layer 1 errors
Properties: Property 9 (region residency), implicit Property 6 (ECR regex).

Two gates live here:

* :func:`check_region_residency` — calls ``s3:GetBucketLocation`` for every S3
  URI referenced by an Input_Manifest and raises :class:`RegionResidencyError`
  naming the offending bucket and its reported region when any bucket lives
  outside ``ap-southeast-1``.
* :func:`check_ecr_residency` — matches each image URI against the
  ``ap-southeast-1`` ECR regex (Property 6 shape) and raises
  :class:`EcrResidencyError` when a URI refers to another region or another
  registry.

Both checks run client-side inside ``scripts/submit-run.py``; Design Decision
D10 explains why residency enforcement cannot live inside the WDL workflow
itself.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping
from urllib.parse import urlparse


AP_SE_1 = "ap-southeast-1"

# Property 6 shape: 12-digit account id, literal ap-southeast-1 ECR host, a
# repository path, and an optional ``@sha256:...`` digest. Digest-pinning is
# enforced by Property 6 at the WDL level; this regex permits an un-pinned URI
# (tag-only or no reference) because :func:`check_ecr_residency` is about
# *residency* of the ECR host, not about pinning.
ECR_URI_RE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/"
    r"(?P<repo>[a-z0-9][a-z0-9/_.-]*)"
    r"(?:"
    r"(?::(?P<tag>[A-Za-z0-9_.-]+))?"
    r"(?:@sha256:(?P<digest>[0-9a-f]{64}))?"
    r")$"
)


class RegionResidencyError(Exception):
    """Raised when any S3 bucket referenced by a manifest lives outside ap-southeast-1."""


class EcrResidencyError(Exception):
    """Raised when any container image URI is not hosted in the ap-southeast-1 ECR."""


# Fields in an Input_Manifest that can carry an ``s3://bucket/key`` URI and so
# must be region-checked. Kept as a tuple (not a set) so error messages have a
# stable iteration order. Fields that are optional on the manifest are simply
# skipped when missing.
_S3_URI_FIELDS: tuple[str, ...] = (
    "hifi_reads_bam",
    "hifi_reads_bai",
    "reference_fasta",
    "reference_fai",
    "output_prefix",
    "input_manifest_json",
    "harmoniser_filter_override_json",
)


def parse_s3_bucket(uri: str) -> str:
    """Return the bucket name from an ``s3://bucket/key`` URI.

    Raises ``ValueError`` if the URI scheme is not ``s3`` or the bucket is
    empty, so malformed manifest values surface as a clear error rather than a
    downstream ``ClientError`` from the S3 SDK.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Not an s3:// URI: {uri!r}")
    if not parsed.netloc:
        raise ValueError(f"s3 URI missing bucket: {uri!r}")
    return parsed.netloc


def _resolve_region(response: Mapping[str, object]) -> str:
    """Normalise a ``GetBucketLocation`` response to a region string.

    Historically ``GetBucketLocation`` returns ``LocationConstraint=None`` for
    ``us-east-1`` and for buckets created before location constraints were
    tracked. The ``ap-southeast-1`` gate must refuse those buckets too, so the
    missing/None case is surfaced as the literal ``"us-east-1"`` rather than
    silently matched against ``ap-southeast-1``.
    """
    constraint = response.get("LocationConstraint")
    if constraint in (None, "", "null"):
        return "us-east-1"
    if not isinstance(constraint, str):  # defensive; boto3 always returns str|None
        raise TypeError(
            f"Unexpected LocationConstraint type {type(constraint).__name__}"
        )
    # boto3 returns ``EU`` for some legacy eu-west-1 buckets; keep the raw
    # string and let the caller compare. AP_SE_1 will still be the literal
    # "ap-southeast-1" for in-region buckets.
    return constraint


def check_region_residency(manifest: Mapping[str, object], s3_client) -> None:
    """Raise ``RegionResidencyError`` if any S3 URI in *manifest* is outside ap-southeast-1.

    Iterates over :data:`_S3_URI_FIELDS`, skipping missing/null values, and
    calls ``s3_client.get_bucket_location`` once per unique bucket (repeated
    lookups for the same bucket are cached to avoid redundant API calls).

    The exception message names the first offending ``(bucket, field, region)``
    triple — Property 9 only requires at least one offending bucket to be
    identified, so stopping at the first mismatch keeps the error message
    compact without hiding information.
    """
    cache: dict[str, str] = {}
    for field in _S3_URI_FIELDS:
        value = manifest.get(field)
        if not value or not isinstance(value, str):
            continue
        bucket = parse_s3_bucket(value)
        region = cache.get(bucket)
        if region is None:
            resp = s3_client.get_bucket_location(Bucket=bucket)
            region = _resolve_region(resp)
            cache[bucket] = region
        if region != AP_SE_1:
            raise RegionResidencyError(
                f"Bucket {bucket!r} referenced by manifest field {field!r} "
                f"is in region {region!r}, required {AP_SE_1!r}"
            )


def check_ecr_residency(image_uris: Iterable[str]) -> None:
    """Raise ``EcrResidencyError`` when any URI in *image_uris* is not in ap-southeast-1 ECR.

    Each URI is matched against :data:`ECR_URI_RE`. The canonical form is
    ``<account>.dkr.ecr.ap-southeast-1.amazonaws.com/<repo>[:tag][@sha256:...]``.

    Rejection is eager (first mismatch raises); the message names the offending
    URI and the expected regex so an operator can spot typos in
    ``containers/manifest.yaml`` quickly.
    """
    for uri in image_uris:
        if not isinstance(uri, str) or not ECR_URI_RE.match(uri):
            raise EcrResidencyError(
                f"Image URI {uri!r} is not hosted in ap-southeast-1 ECR "
                f"(expected match against {ECR_URI_RE.pattern!r})"
            )
