"""Unit tests for ``scripts/mirror-images.py`` manifest parsing and digest
rewriting (Task 2.3).

These tests validate Requirements: 8.3 (manifest must declare name/upstream/
ecr_repo/tag/platforms/digests) via the parser and rewrite helpers exposed
by ``mirror-images.py``. They are pure-Python: no docker, no boto3 calls,
no AWS.

The script lives at ``scripts/mirror-images.py`` — a filename with a hyphen
that cannot be imported with a normal ``import`` statement, so we load it
via :mod:`importlib.util` and bind it to the local name ``mirror_images``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import the hyphenated script as a module named ``mirror_images``.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "mirror-images.py"
)


def _load_mirror_images_module():
    """Load ``scripts/mirror-images.py`` as a module and cache it."""
    if "mirror_images" in sys.modules:
        return sys.modules["mirror_images"]
    spec = importlib.util.spec_from_file_location(
        "mirror_images", str(_SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None, (
        f"Could not build import spec for {_SCRIPT_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["mirror_images"] = module
    spec.loader.exec_module(module)
    return module


mirror_images = _load_mirror_images_module()


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------


_VALID_FIXTURE = """\
images:
  - name: hifiasm
    upstream: quay.io/biocontainers/hifiasm:0.19.9
    ecr_repo: aou-sv/hifiasm
    tag: 0.19.9
    platforms: [linux/amd64, linux/arm64]
    digest_amd64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY
    digest_arm64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY

  - name: pbsv
    upstream: quay.io/biocontainers/pbsv:2.9.0
    ecr_repo: aou-sv/pbsv
    tag: 2.9.0
    platforms: [linux/amd64]
    digest_amd64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY
"""


def test_load_manifest_accepts_valid_fixture(tmp_path: Path) -> None:
    """A minimally-valid manifest with 2 images parses successfully and the
    returned dict exposes the ``images`` list with 2 entries."""
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(_VALID_FIXTURE, encoding="utf-8")

    data = mirror_images.load_manifest(manifest_path)

    assert isinstance(data, dict)
    assert "images" in data
    assert isinstance(data["images"], list)
    assert len(data["images"]) == 2
    assert data["images"][0]["name"] == "hifiasm"
    assert data["images"][1]["name"] == "pbsv"


def test_load_manifest_rejects_missing_top_level(tmp_path: Path) -> None:
    """A YAML document with no ``images:`` key is rejected with a
    ``ValueError`` whose message mentions ``images``."""
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        "# no images key here\nsomething_else: []\n", encoding="utf-8"
    )

    with pytest.raises(ValueError) as excinfo:
        mirror_images.load_manifest(manifest_path)
    assert "images" in str(excinfo.value)


def test_load_manifest_rejects_empty_images_list(tmp_path: Path) -> None:
    """``images: []`` is structurally valid YAML but semantically useless;
    ``load_manifest`` must raise ``ValueError``."""
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text("images: []\n", encoding="utf-8")

    with pytest.raises(ValueError):
        mirror_images.load_manifest(manifest_path)


# ---------------------------------------------------------------------------
# rewrite_manifest_digests
# ---------------------------------------------------------------------------


# Hand-built manifest string with: a comment header, two image entries,
# blank separator lines, and digest placeholders on both platforms. This
# lets us assert strict byte-for-byte preservation of everything that is
# not a targeted digest line.
_REWRITE_FIXTURE = (
    "# Comment header kept verbatim\n"
    "# Another comment line\n"
    "\n"
    "images:\n"
    "  - name: hifiasm\n"
    "    upstream: quay.io/biocontainers/hifiasm:0.19.9\n"
    "    ecr_repo: aou-sv/hifiasm\n"
    "    tag: 0.19.9\n"
    "    platforms: [linux/amd64, linux/arm64]\n"
    "    digest_amd64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY\n"
    "    digest_arm64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY\n"
    "\n"
    "  - name: pbsv\n"
    "    upstream: quay.io/biocontainers/pbsv:2.9.0\n"
    "    ecr_repo: aou-sv/pbsv\n"
    "    tag: 2.9.0\n"
    "    platforms: [linux/amd64]\n"
    "    digest_amd64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY\n"
)


def _write_fixture(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_digest_rewrite_preserves_non_digest_lines(tmp_path: Path) -> None:
    """Rewriting a subset of digests leaves every non-digest line
    byte-identical; targeted digest lines carry the new digest with the
    original ``    digest_<plat>: `` prefix intact; un-targeted digest
    lines remain at the placeholder value."""
    path = _write_fixture(tmp_path, _REWRITE_FIXTURE)
    original_lines = _REWRITE_FIXTURE.splitlines(keepends=True)

    new_hifiasm_amd64 = "sha256:" + "a" * 64
    new_pbsv_amd64 = "sha256:" + "b" * 64
    updates = {
        ("hifiasm", "amd64"): new_hifiasm_amd64,
        ("pbsv", "amd64"): new_pbsv_amd64,
    }
    mirror_images.rewrite_manifest_digests(path, updates)

    rewritten = path.read_text(encoding="utf-8")
    rewritten_lines = rewritten.splitlines(keepends=True)

    # Total line count must be preserved.
    assert len(rewritten_lines) == len(original_lines)

    for i, (orig, new) in enumerate(zip(original_lines, rewritten_lines)):
        orig_stripped = orig.rstrip("\n")
        # Identify which lines we expect to have been rewritten.
        if orig_stripped == "    digest_amd64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY":
            # Both hifiasm and pbsv had their amd64 digests updated.
            assert new.startswith("    digest_amd64: "), (
                f"line {i}: prefix not preserved: {new!r}"
            )
            # The rewritten line ends with one of the two new digests.
            assert new.rstrip("\n").endswith(new_hifiasm_amd64) or new.rstrip(
                "\n"
            ).endswith(new_pbsv_amd64), (
                f"line {i}: rewritten digest not one of the expected values: {new!r}"
            )
        elif orig_stripped == "    digest_arm64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY":
            # arm64 was not in the update dict — must be unchanged.
            assert new == orig, (
                f"line {i}: un-updated arm64 digest was modified: {new!r}"
            )
        else:
            # Every non-digest line is byte-identical.
            assert new == orig, (
                f"line {i}: non-digest line was modified: {new!r} (was {orig!r})"
            )


def test_digest_rewrite_is_noop_when_no_matches(tmp_path: Path) -> None:
    """An update dict that names an image or platform not present in the
    file leaves the file bytes completely unchanged."""
    path = _write_fixture(tmp_path, _REWRITE_FIXTURE)
    before = path.read_bytes()

    updates = {
        ("does-not-exist", "amd64"): "sha256:" + "c" * 64,
        ("hifiasm", "s390x"): "sha256:" + "d" * 64,  # valid image, bad platform
    }
    mirror_images.rewrite_manifest_digests(path, updates)

    after = path.read_bytes()
    assert after == before


def test_digest_rewrite_handles_trailing_whitespace(tmp_path: Path) -> None:
    """If the input digest line has trailing whitespace before the newline
    (e.g. a stray ``   `` after the placeholder), rewrite_manifest_digests
    must preserve that trailing whitespace — the regex captures it in the
    ``trailing`` group and the rewriter re-emits it unchanged."""
    content = (
        "images:\n"
        "  - name: hifiasm\n"
        "    tag: 0.19.9\n"
        "    platforms: [linux/amd64]\n"
        "    digest_amd64: sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY   \n"
    )
    path = _write_fixture(tmp_path, content)

    new_digest = "sha256:" + "e" * 64
    mirror_images.rewrite_manifest_digests(path, {("hifiasm", "amd64"): new_digest})

    rewritten = path.read_text(encoding="utf-8")
    # The rewritten digest line must end with three spaces + newline.
    assert rewritten.endswith(f"    digest_amd64: {new_digest}   \n"), (
        f"trailing whitespace not preserved: {rewritten!r}"
    )


# ---------------------------------------------------------------------------
# Regex unit tests
# ---------------------------------------------------------------------------


def test_name_line_regex_accepts_valid_names() -> None:
    """``_NAME_LINE_RE`` matches valid ``- name: <tool>`` lines with
    alphanumerics, underscores, and hyphens, and captures the tool name in
    group 1. It must NOT match lines missing the leading hyphen or lines
    whose key is something other than ``name``."""
    name_re = mirror_images._NAME_LINE_RE

    # Positive cases — matches and captures the tool name.
    for line, expected_name in (
        ("- name: hifiasm", "hifiasm"),
        ("  - name: pav2svs", "pav2svs"),
        ("  - name: metadata-writer", "metadata-writer"),
    ):
        m = name_re.match(line)
        assert m is not None, f"expected match for {line!r}"
        assert m.group(1) == expected_name, (
            f"expected capture {expected_name!r} for {line!r}, got {m.group(1)!r}"
        )

    # Negative cases — missing hyphen or wrong key.
    assert name_re.match("  name: foo") is None, (
        "regex must not match lines missing the leading '-'"
    )
    assert name_re.match("- version: 1.0") is None, (
        "regex must not match lines whose key is not 'name'"
    )


def test_digest_line_regex() -> None:
    """``_DIGEST_LINE_RE`` matches ``<indent>digest_<plat>: sha256:...``
    lines and captures the prefix, platform suffix, digest, and trailing
    whitespace groups. It must NOT match lines where the digest key has
    been commented out."""
    digest_re = mirror_images._DIGEST_LINE_RE

    line = "    digest_amd64: sha256:abc123"
    m = digest_re.match(line)
    assert m is not None, f"expected match for {line!r}"
    assert m.group("prefix") == "    digest_amd64: "
    assert m.group("suffix") == "amd64"
    assert m.group("digest") == "sha256:abc123"
    assert m.group("trailing") == ""

    # arm64 with trailing whitespace — trailing group captures the spaces.
    line_ws = "    digest_arm64: sha256:deadbeef   "
    m_ws = digest_re.match(line_ws)
    assert m_ws is not None
    assert m_ws.group("suffix") == "arm64"
    assert m_ws.group("digest") == "sha256:deadbeef"
    assert m_ws.group("trailing") == "   "

    # Round-trip: prefix + digest + trailing reconstructs the original line.
    assert (
        m_ws.group("prefix") + m_ws.group("digest") + m_ws.group("trailing")
        == line_ws
    )

    # Commented-out digest lines must NOT match.
    assert digest_re.match("    # digest_amd64: sha256:abc123") is None, (
        "regex must not match commented-out digest lines"
    )
