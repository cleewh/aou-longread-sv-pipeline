"""stage_test_data package — test-data staging helpers for scripts/stage-test-data.py.

Task 12: HealthOmics E2E test fixtures (HG002 chr20 HiFi BAM, GRCh38
reference, and the GIAB v0.6 Tier 1 truth set) must be staged into an
operator-supplied ap-southeast-1 S3 bucket before the Requirement 14
end-to-end benchmark can run. The :mod:`stage_test_data.upload` submodule
implements the idempotent, checksum-safe upload logic; the CLI wrapper at
``scripts/stage-test-data.py`` drives it over ``test/e2e/inputs.json``.
"""
