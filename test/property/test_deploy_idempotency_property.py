# Feature: aou-longread-sv-pipeline, Property 12: Deploy script idempotency decision is correct
"""Property-based tests for :func:`deploy.decide_action` (Task 11.2).

**Validates: Requirement 13.2**

Property 12: *for any (name, version, existing_list, force_flag) tuple,
:func:`deploy.decide_action` SHALL return*

* ``"create"`` when no entry of ``existing_list`` has both
  ``entry["name"] == name`` AND ``entry["version"] == version``;
* ``"update"`` when at least one matching entry exists AND
  ``force_flag`` is truthy;
* ``"skip"``   when at least one matching entry exists AND
  ``force_flag`` is falsy.

The decision is pure (no AWS calls, no I/O), so Hypothesis exhausts the
semantic space with random ``existing`` lists plus controlled
``name``/``version`` collisions.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st


# ``scripts/deploy.py`` is not a package, so import by path.
_DEPLOY_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "deploy.py"
)
_spec = importlib.util.spec_from_file_location("_aou_deploy", _DEPLOY_PATH)
assert _spec is not None and _spec.loader is not None
_deploy = importlib.util.module_from_spec(_spec)
sys.modules["_aou_deploy"] = _deploy
_spec.loader.exec_module(_deploy)

decide_action = _deploy.decide_action


# Names and versions are small printable strings; we don't need unicode for
# the semantic test. Versions look like semver but the decision only cares
# about string equality, so we keep the strategy general.
_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
workflow_name = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=40)
semver_version = st.from_regex(r"^[0-9]+\.[0-9]+\.[0-9]+\Z", fullmatch=True)


@st.composite
def existing_workflows(draw):
    """Build a list of 0..10 ``{"name", "version", "id"}`` dicts.

    Every entry carries an opaque ``id`` so the shape matches what
    ``aws omics list-workflows`` returns after it's been mapped through
    ``_existing_for_decide``.
    """
    return draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "name": workflow_name,
                    "version": semver_version,
                    "id": st.text(
                        alphabet=_NAME_ALPHABET, min_size=1, max_size=16
                    ),
                }
            ),
            min_size=0,
            max_size=10,
        )
    )


@pytest.mark.property_test
@given(
    name=workflow_name,
    version=semver_version,
    existing=existing_workflows(),
    force_flag=st.booleans(),
)
@settings(max_examples=100)
def test_decide_action_matches_property_12(name, version, existing, force_flag):
    """decide_action returns create/update/skip per the Property 12 truth table."""
    action = decide_action(name, version, existing, force_flag)

    has_match = any(
        entry.get("name") == name and entry.get("version") == version
        for entry in existing
    )

    if not has_match:
        assert action == "create"
    elif force_flag:
        assert action == "update"
    else:
        assert action == "skip"


@pytest.mark.property_test
@given(
    name=workflow_name,
    version=semver_version,
    force_flag=st.booleans(),
)
@settings(max_examples=50)
def test_decide_action_creates_when_existing_empty(name, version, force_flag):
    """An empty existing list always yields ``create`` regardless of force_flag."""
    assert decide_action(name, version, [], force_flag) == "create"


@pytest.mark.property_test
@given(
    name=workflow_name,
    version=semver_version,
    other_existing=existing_workflows(),
)
@settings(max_examples=100)
def test_force_true_on_match_always_updates(name, version, other_existing):
    """When a match exists, force_flag=True always yields ``update``."""
    existing = list(other_existing) + [
        {"name": name, "version": version, "id": "wfl-test"}
    ]
    assert decide_action(name, version, existing, True) == "update"


@pytest.mark.property_test
@given(
    name=workflow_name,
    version=semver_version,
    other_existing=existing_workflows(),
)
@settings(max_examples=100)
def test_force_false_on_match_always_skips(name, version, other_existing):
    """When a match exists, force_flag=False always yields ``skip``."""
    existing = list(other_existing) + [
        {"name": name, "version": version, "id": "wfl-test"}
    ]
    assert decide_action(name, version, existing, False) == "skip"
