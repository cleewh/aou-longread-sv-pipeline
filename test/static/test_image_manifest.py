# Feature: aou-longread-sv-pipeline, Property 7: Image_Manifest entries are complete
"""Static completeness check for ``containers/manifest.yaml`` (Task 15.2).

**Validates: Requirement 8.3**

Every entry in the image manifest must carry:

* non-empty ``name``, ``upstream``, ``ecr_repo``, and ``tag`` strings,
* a non-empty ``platforms`` list,
* one ``digest_<platform>`` key per entry in ``platforms``, and that
  key's value must be a non-empty string — either the sentinel
  ``sha256:TO_BE_FILLED_BY_MIRROR_IMAGES_PY`` (before the first
  ``mirror-images.py`` run) or a real ``sha256:<64-hex>`` value after.

Sentinel placeholders are ACCEPTED here because property 7 is a
completeness property ("every listed platform has a digest field")
not a content property; ``scripts/mirror-images.py`` rewrites the
sentinel with a real digest when the images are pushed (Task 22). The
real-digest content check is covered by the Property 17 test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MANIFEST_PATH = _REPO_ROOT / "containers" / "manifest.yaml"

_REQUIRED_STRING_KEYS = ("name", "upstream", "ecr_repo", "tag")


def _load_manifest() -> list[dict]:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and "images" in raw, (
        f"{_MANIFEST_PATH}: top-level document must be a mapping with 'images' key"
    )
    images = raw["images"]
    assert isinstance(images, list) and images, (
        f"{_MANIFEST_PATH}: 'images' must be a non-empty list"
    )
    return images


@pytest.mark.property_test
def test_every_entry_has_required_string_fields():
    for entry in _load_manifest():
        for key in _REQUIRED_STRING_KEYS:
            value = entry.get(key)
            assert isinstance(value, str) and value, (
                f"manifest entry {entry!r}: {key!r} must be a non-empty string "
                f"(got {value!r})"
            )


@pytest.mark.property_test
def test_every_entry_has_non_empty_platforms_list():
    for entry in _load_manifest():
        platforms = entry.get("platforms")
        assert isinstance(platforms, list) and platforms, (
            f"manifest entry {entry.get('name', entry)!r}: platforms must be a "
            f"non-empty list (got {platforms!r})"
        )
        for platform in platforms:
            assert isinstance(platform, str) and platform, (
                f"manifest entry {entry.get('name', entry)!r}: platforms entry "
                f"{platform!r} must be a non-empty string"
            )


@pytest.mark.property_test
def test_every_platform_has_matching_digest_field():
    """Property 7: at least one digest per listed platform."""
    for entry in _load_manifest():
        name = entry.get("name", entry)
        platforms = entry.get("platforms", [])
        for platform in platforms:
            # ``linux/amd64`` -> ``digest_amd64``; ``linux/arm64`` -> ``digest_arm64``.
            arch = platform.split("/")[-1]
            digest_key = f"digest_{arch}"
            digest_value = entry.get(digest_key)
            assert isinstance(digest_value, str) and digest_value, (
                f"manifest entry {name!r}: platforms lists {platform!r} but "
                f"{digest_key!r} is missing or empty (got {digest_value!r})"
            )
