# Feature: aou-longread-sv-pipeline, Property 13: Staging script is checksum-safe and idempotent
"""Property-based tests for :mod:`stage_test_data.upload` (Task 12.2).

**Validates: Requirements 15.4, 15.5, 15.6**

Property 13: *for any object listed in ``test/e2e/inputs.json`` and any
observed ``(size, sha256)`` pair for that object in the target bucket,
:func:`stage_test_data.upload.stage_object` SHALL upload the object if
and only if the object is absent from the target bucket OR its observed
``(size, sha256)`` does not match the expected values recorded in
``inputs.json``. Additionally, when an upstream source is unreachable,
the script SHALL exit with a non-zero status AND SHALL NOT overwrite any
already-staged object.*

All tests use in-memory fake S3 and HTTP clients — no network, no real
S3. The property we're probing is pure idempotence + checksum safety, so
mocking the I/O layer keeps the run time bounded (max 50 examples per
property; fixture setup is the expensive part).
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

# Ensure the ``scripts/`` path is on sys.path so ``stage_test_data`` imports.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from stage_test_data.upload import (  # noqa: E402
    ChecksumMismatchError,
    UpstreamUnreachableError,
    stage_object_in_memory,
)


# ---------------------------------------------------------------------------
# Fake S3 + HTTP clients
# ---------------------------------------------------------------------------


class _NotFound(Exception):
    """Stand-in for boto3 ClientError with Code='404'."""

    def __init__(self):
        super().__init__("404 Not Found")
        self.response = {"Error": {"Code": "404"}}


class FakeS3Client:
    """In-memory S3 stub that supports head_object / get_object / put_object."""

    def __init__(self) -> None:
        # bucket -> key -> {"Body": bytes, "Metadata": dict, "ContentLength": int}
        self.objects: dict[tuple[str, str], dict] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.head_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str]] = []
        # Keys we should pretend don't exist on GetObject (simulate upstream S3
        # unreachability). head_object still sees its recorded state.
        self.upstream_blocked: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    def put_object(self, *, Bucket, Key, Body, ContentLength=None, Metadata=None):
        self.put_calls.append((Bucket, Key))
        if hasattr(Body, "read"):
            data = Body.read()
        else:
            data = Body
        self.objects[(Bucket, Key)] = {
            "Body": data,
            "Metadata": dict(Metadata or {}),
            "ContentLength": ContentLength if ContentLength is not None else len(data),
        }
        return {"ETag": "fake"}

    def head_object(self, *, Bucket, Key):
        self.head_calls.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise _NotFound()
        obj = self.objects[(Bucket, Key)]
        return {
            "ContentLength": obj["ContentLength"],
            "Metadata": obj["Metadata"],
        }

    def get_object(self, *, Bucket, Key):
        self.get_calls.append((Bucket, Key))
        if (Bucket, Key) in self.upstream_blocked:
            raise _NotFound()
        if (Bucket, Key) not in self.objects:
            raise _NotFound()
        obj = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(obj["Body"])}


class FakeHttpClient:
    """In-memory HTTP/FTP stub with urlopen(url) -> file-like."""

    def __init__(self) -> None:
        self.responses: dict[str, bytes] = {}
        self.unreachable: set[str] = set()
        self.calls: list[str] = []

    def urlopen(self, url, timeout: float = 60.0):  # noqa: D401
        self.calls.append(url)
        if url in self.unreachable:
            raise OSError(f"simulated network failure: {url}")
        if url not in self.responses:
            raise OSError(f"404: {url}")
        return io.BytesIO(self.responses[url])


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


_BUCKET = "aou-e2e-test-bucket"
_KEY = "hg002/chr20/reads.bam"


def _entry(data: bytes, upstream_uri: str = "https://example.org/reads.bam") -> dict:
    return {
        "s3_uri": f"s3://{_BUCKET}/{_KEY}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "upstream_uri": upstream_uri,
    }


# ---------------------------------------------------------------------------
# Property 13 tests
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@given(
    payload=st.binary(min_size=0, max_size=4096),
)
@settings(max_examples=50)
def test_skips_when_target_matches_expected(payload):
    """If target object is present with matching size+sha256, return 'skipped' and don't call put_object."""
    s3 = FakeS3Client()
    http = FakeHttpClient()
    entry = _entry(payload)
    # Seed the bucket with the exact object we'd expect.
    s3.objects[(_BUCKET, _KEY)] = {
        "Body": payload,
        "Metadata": {"sha256": entry["sha256"]},
        "ContentLength": len(payload),
    }

    result = stage_object_in_memory(entry, _BUCKET, s3, http)

    assert result["status"] == "skipped"
    assert result["key"] == _KEY
    # Idempotence: no upload happened.
    assert s3.put_calls == []
    # No upstream network call either.
    assert http.calls == []


@pytest.mark.property_test
@given(
    payload=st.binary(min_size=1, max_size=4096),
)
@settings(max_examples=50)
def test_uploads_when_target_absent(payload):
    """If target absent, download + upload with recorded sha256 metadata."""
    s3 = FakeS3Client()
    http = FakeHttpClient()
    entry = _entry(payload)
    http.responses[entry["upstream_uri"]] = payload

    result = stage_object_in_memory(entry, _BUCKET, s3, http)

    assert result["status"] == "uploaded"
    assert result["key"] == _KEY
    assert result["size"] == len(payload)
    assert result["sha256"] == entry["sha256"]
    assert s3.put_calls == [(_BUCKET, _KEY)]
    assert s3.objects[(_BUCKET, _KEY)]["Metadata"]["sha256"] == entry["sha256"]


@pytest.mark.property_test
@given(
    payload=st.binary(min_size=1, max_size=4096),
    seeded_with_match=st.booleans(),
)
@settings(max_examples=50)
def test_upstream_unreachable_never_overwrites(payload, seeded_with_match):
    """Upstream unreachable → raises UpstreamUnreachableError, target key untouched.

    We explore two initial states:
    * target already has a *matching* object (then stage_object skips before
      ever touching the upstream — no raise expected);
    * target is absent or mismatched (then stage_object must reach out,
      fail, and leave the bucket exactly as it was found).
    """
    s3 = FakeS3Client()
    http = FakeHttpClient()
    entry = _entry(payload)
    # Upstream is unreachable — both the (possibly) matching S3 source AND
    # the HTTP source are blocked so we truly can't fetch.
    http.unreachable.add(entry["upstream_uri"])

    pre_state: dict[tuple[str, str], dict] = {}
    if seeded_with_match:
        s3.objects[(_BUCKET, _KEY)] = {
            "Body": payload,
            "Metadata": {"sha256": entry["sha256"]},
            "ContentLength": len(payload),
        }
        pre_state = {k: dict(v) for k, v in s3.objects.items()}
    else:
        # Seed with a DIFFERENT object (wrong sha) to prove it's not clobbered.
        stale_data = b"STALE-SENTINEL"
        s3.objects[(_BUCKET, _KEY)] = {
            "Body": stale_data,
            "Metadata": {"sha256": hashlib.sha256(stale_data).hexdigest()},
            "ContentLength": len(stale_data),
        }
        pre_state = {k: dict(v) for k, v in s3.objects.items()}

    if seeded_with_match:
        # Skip path wins — no network call, no raise.
        result = stage_object_in_memory(entry, _BUCKET, s3, http)
        assert result["status"] == "skipped"
        assert s3.put_calls == []
    else:
        with pytest.raises(UpstreamUnreachableError) as excinfo:
            stage_object_in_memory(entry, _BUCKET, s3, http)
        assert entry["upstream_uri"] in str(excinfo.value)
        # Most important invariant (Requirement 15.5): no overwrite.
        assert (_BUCKET, _KEY) not in [
            (b, k) for (b, k) in s3.put_calls
        ], f"put_object called on stale key {_KEY}!"

    # Stale object is still exactly as we left it.
    assert s3.objects == pre_state


@pytest.mark.property_test
@given(
    real_payload=st.binary(min_size=1, max_size=4096),
    fake_payload=st.binary(min_size=1, max_size=4096),
)
@settings(max_examples=50)
def test_checksum_mismatch_raises(real_payload, fake_payload):
    """Downloaded checksum mismatches expected → ChecksumMismatchError."""
    # Require the payloads to differ so the mismatch is real.
    if hashlib.sha256(real_payload).hexdigest() == hashlib.sha256(fake_payload).hexdigest():
        return
    if len(real_payload) == len(fake_payload) and real_payload == fake_payload:
        return

    s3 = FakeS3Client()
    http = FakeHttpClient()
    # entry records ``real_payload``'s size+sha, but the upstream actually
    # serves ``fake_payload`` — the staging routine must catch the mismatch.
    entry = _entry(real_payload)
    http.responses[entry["upstream_uri"]] = fake_payload

    with pytest.raises(ChecksumMismatchError) as excinfo:
        stage_object_in_memory(entry, _BUCKET, s3, http)
    assert entry["s3_uri"] in str(excinfo.value)
    # Target bucket must not have been overwritten with the bad data.
    # A put_object call is allowed only when both size AND sha matched.
    assert (_BUCKET, _KEY) not in s3.objects


# ---------------------------------------------------------------------------
# A combined "state × upstream" scan to exercise the whole truth table in one
# Hypothesis run — guarding against regressions where a code path succeeds in
# isolation but breaks when composed (e.g. sha check disabled when the
# bucket had a stale object).
# ---------------------------------------------------------------------------


@pytest.mark.property_test
@given(
    payload=st.binary(min_size=1, max_size=1024),
    target_state=st.sampled_from(["absent", "match", "wrong_sha", "wrong_size"]),
    upstream_state=st.sampled_from(["reachable", "unreachable"]),
)
@settings(max_examples=50)
def test_combined_state_transitions(payload, target_state, upstream_state):
    """Full truth table: (target_state × upstream_state) → correct outcome."""
    s3 = FakeS3Client()
    http = FakeHttpClient()
    entry = _entry(payload)

    # Seed target state.
    if target_state == "match":
        s3.objects[(_BUCKET, _KEY)] = {
            "Body": payload,
            "Metadata": {"sha256": entry["sha256"]},
            "ContentLength": len(payload),
        }
    elif target_state == "wrong_sha":
        stale = b"STALE-" + payload
        s3.objects[(_BUCKET, _KEY)] = {
            "Body": stale,
            "Metadata": {"sha256": hashlib.sha256(stale).hexdigest()},
            "ContentLength": len(payload),  # same size but wrong sha
        }
    elif target_state == "wrong_size":
        stale = payload + b"X"
        s3.objects[(_BUCKET, _KEY)] = {
            "Body": stale,
            "Metadata": {"sha256": hashlib.sha256(stale).hexdigest()},
            "ContentLength": len(stale),
        }
    # "absent" → leave the bucket empty.

    pre_put_count = len(s3.put_calls)

    # Seed upstream.
    if upstream_state == "reachable":
        http.responses[entry["upstream_uri"]] = payload
    else:
        http.unreachable.add(entry["upstream_uri"])

    if target_state == "match":
        # Skip path always wins; upstream state irrelevant.
        result = stage_object_in_memory(entry, _BUCKET, s3, http)
        assert result["status"] == "skipped"
        assert len(s3.put_calls) == pre_put_count  # no new upload
        return

    if upstream_state == "unreachable":
        with pytest.raises(UpstreamUnreachableError):
            stage_object_in_memory(entry, _BUCKET, s3, http)
        # Requirement 15.5: don't overwrite the stale object.
        assert len(s3.put_calls) == pre_put_count
    else:
        result = stage_object_in_memory(entry, _BUCKET, s3, http)
        assert result["status"] == "uploaded"
        # The upload DID happen; the stale object (if any) has been replaced
        # with the correct bytes.
        assert s3.objects[(_BUCKET, _KEY)]["Body"] == payload
        assert s3.objects[(_BUCKET, _KEY)]["Metadata"]["sha256"] == entry["sha256"]
