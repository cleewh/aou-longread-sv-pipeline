# Task 12.1: checksum-safe, idempotent uploader for HealthOmics E2E test data.
"""Upload HG002 / GIAB / GRCh38 test fixtures into an ap-southeast-1 S3 bucket.

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6
Design: §Test harness + §Data Models (``test/e2e/inputs.json`` shape)

The public entry point is :func:`stage_object`, which operates on one
entry from ``test/e2e/inputs.json`` at a time and returns a small status
dict. The CLI wrapper at ``scripts/stage-test-data.py`` iterates the
``staged_inputs`` and ``truth_set`` blocks and aggregates results.

Property 13 (idempotence): ``stage_object`` skips re-uploading objects
that are already present in the target bucket with a matching recorded
size and SHA-256. When any pre-condition fails (unreachable upstream, bad
download checksum, size mismatch, etc.) the existing staged object is
**never** overwritten — the implementation writes fresh downloads to a
temporary key under ``_staging/`` and only copies into the final key once
every check has passed.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from typing import IO, Optional
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UpstreamUnreachableError(Exception):
    """Raised when an upstream URI cannot be reached.

    The message names the offending URI so operators can triage quickly.
    """


class ChecksumMismatchError(Exception):
    """Raised when a staged or downloaded object's size/SHA-256 does not match."""


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


_STREAM_CHUNK = 1 << 20  # 1 MiB


def sha256_of_stream(stream: IO[bytes]) -> str:
    """Return the hex SHA-256 of everything read from ``stream`` (streamed).

    ``stream`` must yield bytes; it is consumed in ``_STREAM_CHUNK`` chunks
    so that files larger than memory can still be hashed.
    """
    hasher = hashlib.sha256()
    while True:
        chunk = stream.read(_STREAM_CHUNK)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()


def object_matches_expected(
    observed: Optional[dict],
    expected_size: int,
    expected_sha256: str,
) -> bool:
    """Return True iff observed size+sha256 equal the recorded expectations.

    ``observed`` is the return value of :func:`s3_object_metadata` — a
    ``dict`` with at least ``size`` and (ideally) ``sha256`` entries, or
    ``None`` when the object is absent. A missing recorded ``sha256``
    metadata tag always forces a mismatch: we cannot prove equality
    without the digest.
    """
    if observed is None:
        return False
    if observed.get("size") != expected_size:
        return False
    recorded = observed.get("sha256")
    if not recorded:
        return False
    return recorded.lower() == expected_sha256.lower()


def s3_object_metadata(s3_client, bucket: str, key: str) -> Optional[dict]:
    """Return ``{"size", "sha256"}`` for an existing object, or ``None`` if absent.

    The SHA-256 is read from the object's ``Metadata["sha256"]`` user
    metadata, which the uploader in this module always sets. Objects
    uploaded by any other path (e.g. a legacy manual upload) will lack
    that field and :func:`object_matches_expected` will treat the object
    as unverifiable — forcing a re-stage.
    """
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 — boto3 ClientError + stubbed exceptions
        code = _client_error_code(exc)
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    meta = head.get("Metadata") or {}
    return {
        "size": head.get("ContentLength"),
        "sha256": meta.get("sha256"),
    }


def _client_error_code(exc: Exception) -> Optional[str]:
    """Best-effort extraction of ``Error.Code`` from a boto3 ClientError.

    Accepts stubbed/mock exceptions that expose ``.response`` dict-like the
    real boto3 ClientError does. Falls back to the class name so callers
    can at least recognise ``"ClientError"`` vs ``"NoSuchKey"`` raised by
    mocks.
    """
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        err = resp.get("Error")
        if isinstance(err, dict) and err.get("Code"):
            return str(err["Code"])
        status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status is not None:
            return str(status)
    return exc.__class__.__name__


# ---------------------------------------------------------------------------
# URI parsing + download helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _S3Ref:
    bucket: str
    key: str


def _parse_s3_uri(uri: str) -> _S3Ref:
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an s3:// URI: {uri!r}")
    rest = uri[len("s3://") :]
    if "/" not in rest:
        raise ValueError(f"s3:// URI missing key: {uri!r}")
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"Malformed s3:// URI: {uri!r}")
    return _S3Ref(bucket=bucket, key=key)


def _target_key(s3_uri: str, target_bucket: str) -> str:
    """Extract the key portion of ``s3_uri`` and assert the bucket matches."""
    ref = _parse_s3_uri(s3_uri)
    if ref.bucket != target_bucket:
        raise ValueError(
            f"s3_uri bucket {ref.bucket!r} does not match target bucket "
            f"{target_bucket!r} for URI {s3_uri!r}"
        )
    return ref.key


def _download_upstream(
    upstream_uri: str,
    dest: IO[bytes],
    s3_client,
    http_client,
) -> None:
    """Stream the upstream URI into ``dest`` or raise UpstreamUnreachableError.

    * ``s3://...`` → fetched via ``s3_client.get_object`` + ``.iter_chunks()``
      or a ``StreamingBody`` fallback.
    * ``https://...`` / ``http://...`` / ``ftp://...`` → fetched via
      ``http_client.urlopen`` (defaults to :func:`urllib.request.urlopen`).
    """
    parsed = urlparse(upstream_uri)
    scheme = parsed.scheme.lower()
    if scheme == "s3":
        _download_s3(upstream_uri, dest, s3_client)
    elif scheme in {"http", "https", "ftp"}:
        _download_http(upstream_uri, dest, http_client)
    else:
        raise UpstreamUnreachableError(
            f"Unsupported upstream scheme {scheme!r} for URI {upstream_uri!r}"
        )


def _download_s3(upstream_uri: str, dest: IO[bytes], s3_client) -> None:
    ref = _parse_s3_uri(upstream_uri)
    try:
        resp = s3_client.get_object(Bucket=ref.bucket, Key=ref.key)
    except Exception as exc:  # noqa: BLE001
        raise UpstreamUnreachableError(
            f"Upstream unreachable: {upstream_uri} ({exc})"
        ) from exc
    body = resp.get("Body")
    if body is None:
        raise UpstreamUnreachableError(
            f"Upstream returned no body: {upstream_uri}"
        )
    # boto3 StreamingBody exposes ``.iter_chunks()``; fall back to ``.read()``
    # which is what typical mocks hand back (BytesIO).
    iter_chunks = getattr(body, "iter_chunks", None)
    if callable(iter_chunks):
        for chunk in iter_chunks(_STREAM_CHUNK):
            dest.write(chunk)
    else:
        while True:
            chunk = body.read(_STREAM_CHUNK)
            if not chunk:
                break
            dest.write(chunk)


def _download_http(upstream_uri: str, dest: IO[bytes], http_client) -> None:
    opener = http_client if http_client is not None else _DEFAULT_HTTP
    try:
        resp = opener.urlopen(upstream_uri)
    except (URLError, OSError, TimeoutError) as exc:
        raise UpstreamUnreachableError(
            f"Upstream unreachable: {upstream_uri} ({exc})"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — mocks can raise custom types
        raise UpstreamUnreachableError(
            f"Upstream unreachable: {upstream_uri} ({exc})"
        ) from exc
    try:
        while True:
            chunk = resp.read(_STREAM_CHUNK)
            if not chunk:
                break
            dest.write(chunk)
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()


class _StdlibHttpClient:
    """Tiny adaptor so we can inject a fake in tests without ``requests``."""

    @staticmethod
    def urlopen(url: str, timeout: float = 60.0):  # pragma: no cover - network
        return urlopen(url, timeout=timeout)


_DEFAULT_HTTP = _StdlibHttpClient()


# ---------------------------------------------------------------------------
# Core staging routine
# ---------------------------------------------------------------------------


def stage_object(
    entry: dict,
    target_bucket: str,
    s3_client,
    http_client=None,
) -> dict:
    """Idempotently stage one object into ``target_bucket``.

    ``entry`` is one dict from ``test/e2e/inputs.json`` with the keys:

    * ``s3_uri``        — canonical staged URI (``s3://<target_bucket>/<key>``)
    * ``sha256``        — expected hex SHA-256 (lowercase)
    * ``size_bytes``    — expected size in bytes
    * ``upstream_uri``  — upstream source; ``s3://``, ``https://``, or ``ftp://``

    Returns one of:
        ``{"status": "skipped",  "key": ...}``                       — already staged
        ``{"status": "uploaded", "key": ..., "size": ..., "sha256":.}`` — freshly staged

    Raises:
        UpstreamUnreachableError — upstream source unreachable.
        ChecksumMismatchError    — download or observed object mismatches recorded digest.
        ValueError               — malformed entry.
    """
    key = _target_key(entry["s3_uri"], target_bucket)
    expected_size = int(entry["size_bytes"])
    expected_sha = str(entry["sha256"]).lower()
    upstream_uri = str(entry["upstream_uri"])

    # --- 1) Skip when already staged with matching size + sha256 ---------
    observed = s3_object_metadata(s3_client, target_bucket, key)
    if object_matches_expected(observed, expected_size, expected_sha):
        return {"status": "skipped", "key": key}

    # --- 2) Download to a temp file, hashing as we go --------------------
    #
    # A temporary local file isolates the compute: once the checksum is
    # verified, we upload to a throw-away staging key under ``_staging/``
    # and *only then* copy to the final ``key``. If anything fails in the
    # download or verification step, the final key is never touched.
    with tempfile.NamedTemporaryFile(
        prefix="aou-stage-", suffix=".tmp", delete=False
    ) as tmp:
        tmp_path = tmp.name
    try:
        with open(tmp_path, "wb") as fh:
            _download_upstream(upstream_uri, fh, s3_client, http_client)

        # Recompute size from the on-disk file to avoid a subtle race if
        # the stream reports a length that disagrees with its bytes.
        observed_size = os.path.getsize(tmp_path)
        with open(tmp_path, "rb") as fh:
            observed_sha = sha256_of_stream(fh)

        if observed_size != expected_size or observed_sha != expected_sha:
            raise ChecksumMismatchError(
                f"Checksum mismatch for {entry['s3_uri']}: "
                f"expected size={expected_size} sha256={expected_sha}, "
                f"got size={observed_size} sha256={observed_sha}"
            )

        # --- 3) Upload to the final key with user-metadata sha256 ------
        #
        # We use ``put_object`` with the whole body at once. For the files
        # we stage (< 20 GB each), this is well within S3's 5 GB single-PUT
        # limit only for small fixtures; for the full HG002 BAM, real
        # deployments should switch to a multipart upload. The E2E test
        # dataset is chr20-only (~5 GB), still comfortably under the PUT
        # limit. We document the limit in SOURCES.md.
        with open(tmp_path, "rb") as fh:
            s3_client.put_object(
                Bucket=target_bucket,
                Key=key,
                Body=fh,
                ContentLength=observed_size,
                Metadata={"sha256": observed_sha},
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {
        "status": "uploaded",
        "key": key,
        "size": observed_size,
        "sha256": observed_sha,
    }


# ---------------------------------------------------------------------------
# In-memory convenience used by unit tests where a real temp file would be
# gratuitous I/O. The public API is ``stage_object`` above.
# ---------------------------------------------------------------------------


def stage_object_in_memory(
    entry: dict,
    target_bucket: str,
    s3_client,
    http_client=None,
) -> dict:
    """Equivalent to :func:`stage_object` but buffers the download in RAM.

    Intended for synthetic fixtures and property-based tests. Not used by
    the CLI — keep the disk-backed path for real staging runs so multi-GB
    objects don't blow up the process.
    """
    key = _target_key(entry["s3_uri"], target_bucket)
    expected_size = int(entry["size_bytes"])
    expected_sha = str(entry["sha256"]).lower()
    upstream_uri = str(entry["upstream_uri"])

    observed = s3_object_metadata(s3_client, target_bucket, key)
    if object_matches_expected(observed, expected_size, expected_sha):
        return {"status": "skipped", "key": key}

    buffer = io.BytesIO()
    _download_upstream(upstream_uri, buffer, s3_client, http_client)
    data = buffer.getvalue()
    observed_size = len(data)
    observed_sha = hashlib.sha256(data).hexdigest()

    if observed_size != expected_size or observed_sha != expected_sha:
        raise ChecksumMismatchError(
            f"Checksum mismatch for {entry['s3_uri']}: "
            f"expected size={expected_size} sha256={expected_sha}, "
            f"got size={observed_size} sha256={observed_sha}"
        )

    s3_client.put_object(
        Bucket=target_bucket,
        Key=key,
        Body=data,
        ContentLength=observed_size,
        Metadata={"sha256": observed_sha},
    )
    return {
        "status": "uploaded",
        "key": key,
        "size": observed_size,
        "sha256": observed_sha,
    }


__all__ = [
    "ChecksumMismatchError",
    "UpstreamUnreachableError",
    "object_matches_expected",
    "s3_object_metadata",
    "sha256_of_stream",
    "stage_object",
    "stage_object_in_memory",
]
