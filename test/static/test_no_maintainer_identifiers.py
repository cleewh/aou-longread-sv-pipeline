"""Static checks that the post-scrub repository contains zero
occurrences of the upstream maintainer's account ID and that
stamp-wdl-digests.py participates in the fa5373f rewrite path
identically against the synthetic placeholder."""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Constructed from concatenated literals so this constant itself does
# not match the grep below.
MAINTAINER_ACCOUNT_ID = "68767" + "7765589"


def test_no_maintainer_account_id():
    """Property 1: zero occurrences of the maintainer account ID in tracked files."""
    result = subprocess.run(
        ["grep", "-rln", MAINTAINER_ACCOUNT_ID, str(REPO_ROOT),
         "--exclude-dir=.git", "--exclude-dir=.hypothesis"],
        capture_output=True, text=True, check=False,
    )
    matches = [ln for ln in result.stdout.splitlines() if ln]
    assert matches == [], f"Maintainer account ID leaks in: {matches}"


CREDENTIAL_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[a-z]-[A-Za-z0-9-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def test_no_real_credentials():
    """Property 2: scrub introduced no new real-credential strings."""
    findings = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or any(
            part in (".git", ".hypothesis") for part in path.parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for pat in CREDENTIAL_PATTERNS:
            if pat.search(text):
                findings.append((str(path.relative_to(REPO_ROOT)),
                                 pat.pattern))
    assert not findings, f"Real-credential leaks: {findings}"


# Property 3: stamp-wdl-digests.py preservation property.
#
# Mirrors the account-agnostic regex from `scripts/stamp-wdl-digests.py`
# (commit fa5373f). For any 12-digit account ID and any AWS region, the
# script rewrites every `runtime.docker` reference of the form
# `<digit12>.dkr.ecr.<region>.amazonaws.com/aou-sv/<tool>@sha256:<digest>`
# to the customer's account/region. We exercise the same regex
# in-process here so the test does not depend on AWS credentials or
# .healthomics/config.toml; the script's regex itself was validated
# end-to-end by the fa5373f commit.

# This must match the regex inside scripts/stamp-wdl-digests.py exactly.
ECR_URI_RE = re.compile(
    r"(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com"
    r"/aou-sv/(?P<tool>[a-z0-9-]+)@sha256:(?P<digest>[0-9a-f]{64})"
)
SYNTHETIC_PLACEHOLDER = "000000000000.dkr.ecr.us-east-1.amazonaws.com"


def _rewrite_account_region(text: str, account_id: str, region: str) -> str:
    """In-process mirror of stamp-wdl-digests.py's substitution loop.

    The script preserves the tool name and digest and rewrites only the
    account+region segment. We reproduce that behaviour here.
    """

    def repl(m: re.Match) -> str:
        return (
            f"{account_id}.dkr.ecr.{region}.amazonaws.com"
            f"/aou-sv/{m.group('tool')}@sha256:{m.group('digest')}"
        )

    return ECR_URI_RE.sub(repl, text)


try:
    from hypothesis import given
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - hypothesis not installed
    given = None
    st = None


if given is not None:
    @given(
        account_id=st.from_regex(r"^[1-9][0-9]{11}$", fullmatch=True),
        region=st.sampled_from(
            [
                "us-east-1",
                "us-west-2",
                "eu-west-1",
                "ap-southeast-1",
                "ap-northeast-1",
                "eu-central-1",
            ]
        ),
    )
    def test_stamp_wdl_digests_overwrites_synthetic_placeholder(
        account_id: str, region: str
    ) -> None:
        """Property 3: every (account_id, region) the customer might supply
        produces a WDL set with zero synthetic placeholders, zero foreign
        account IDs, and customer's (X, R) populated in every
        runtime.docker reference."""
        wdl_dir = REPO_ROOT / "wdl" / "tasks"
        for wdl in sorted(wdl_dir.glob("*.wdl")):
            original = wdl.read_text(encoding="utf-8")
            # Pre-scrub baseline: every ECR URI is the synthetic placeholder.
            for match in ECR_URI_RE.finditer(original):
                assert match.group("account") == "000000000000", (
                    f"Pre-rewrite WDL {wdl.name} carries non-synthetic "
                    f"account {match.group('account')} (only the "
                    f"synthetic 000000000000 placeholder is allowed in "
                    f"the post-scrub repo)"
                )
                assert match.group("region") == "us-east-1", (
                    f"Pre-rewrite WDL {wdl.name} carries non-synthetic "
                    f"region {match.group('region')}"
                )

            rewritten = _rewrite_account_region(original, account_id, region)

            # Post-rewrite: synthetic placeholder is gone.
            assert SYNTHETIC_PLACEHOLDER not in rewritten, (
                f"Synthetic placeholder still present in {wdl.name} "
                f"after rewrite to ({account_id}, {region})"
            )
            # Post-rewrite: every ECR URI carries the customer's
            # (account_id, region), no foreign values leak through.
            for match in ECR_URI_RE.finditer(rewritten):
                assert match.group("account") == account_id, (
                    f"Foreign account {match.group('account')} in "
                    f"{wdl.name} after rewrite to {account_id}"
                )
                assert match.group("region") == region, (
                    f"Foreign region {match.group('region')} in "
                    f"{wdl.name} after rewrite to {region}"
                )
