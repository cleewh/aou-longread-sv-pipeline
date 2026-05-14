# End-to-End Smoke Test — HG002 chr20 on HealthOmics `ap-southeast-1`

This directory holds the inputs, thresholds, cost baseline, and orchestration
script for the HealthOmics end-to-end smoke run described in Design §Test
harness (`test/e2e/run_e2e.py`) and Requirements 14.1-14.9 and 15.1-15.7.

The run submits the full AoU long-read SV pipeline against HG002 chr20
PacBio HiFi reads, benchmarks the harmonised SV calls against the GIAB v0.6
Tier-1 truth set with `truvari bench`, and compares total observed cost
against `cost_baseline.json` per Property 20.

## Prerequisites (operator-supplied)

1. An AWS account in `ap-southeast-1` with AWS HealthOmics enabled.
2. A target S3 bucket in `ap-southeast-1` — the defaults in `inputs.json`
   use `aou-longread-sv-687677765589-ap-southeast-1`; substitute your own
   via `--bucket` if different.
3. Valid AWS credentials on the executor (e.g. `aws configure` or
   instance/role credentials).
4. Local tools on the `PATH`:
   - `python3` 3.11+
   - `bcftools` (needed by the harmonised VCF checks)
   - `truvari` (needed by the benchmark step)
   - `docker` with `buildx` (only needed to re-build container images)
5. The IAM execution role created from `iam/execution_role_trust.json` +
   `iam/execution_role_policy.json.tmpl` (rendered via `iam/render.py`).
   Capture its ARN — you'll pass it as `--role-arn` to `run_e2e.py`.

## Workflow

The full sequence is stage → build/push images → deploy → run. Each step is
independently idempotent.

### 1. Stage test data

`scripts/stage-test-data.py` walks `test/e2e/inputs.json` and uploads each
GIAB / broad-references fixture into the target bucket, skipping objects
already present with matching size + SHA-256. On first run it will rewrite
the placeholder `sha256` / `size_bytes` fields with the observed values.

```bash
python3 scripts/stage-test-data.py --bucket aou-longread-sv-687677765589-ap-southeast-1
```

If a GIAB or broad-references upstream URI has moved, the script fails with
`UpstreamUnreachableError` naming the URI. Update the `upstream_uri` field
in `inputs.json`, commit the change, and re-run.

### 2. Build and push container images

```bash
python3 scripts/mirror-images.py --account-id <your-account-id>
```

Multi-arch images (Graviton + x86_64) are pushed to ECR in `ap-southeast-1`
and their per-platform digests are written back to `containers/manifest.yaml`
and appended to `SOURCES.md`.

### 3. Deploy the workflow

```bash
python3 scripts/deploy.py --region ap-southeast-1
```

This idempotently registers `wdl/main.wdl` + `wdl/parameter_template.json`
as a HealthOmics workflow. Capture the printed `workflowId` (looks like
`wfl-xxxxxxxx`). Pass `--with-budget-alarm` if you want the optional
monthly budget CloudFormation stack deployed alongside the workflow.

### 4. Run the end-to-end smoke test

```bash
python3 test/e2e/run_e2e.py \
    --bucket aou-longread-sv-687677765589-ap-southeast-1 \
    --workflow-id wfl-xxxxxxxx \
    --role-arn arn:aws:iam::687677765589:role/AouLongReadSvExecutionRole
```

The script exits `0` iff every assertion in Design §Test harness holds.
On the first failing assertion it exits non-zero and prints
`expected vs observed` on stderr.

`--dry-run` prints the plan (bucket, workflow, thresholds) without
touching HealthOmics.

## Files in this directory

| File | Purpose |
| ---- | ------- |
| `inputs.json` | Staged S3 URIs, upstream URIs, checksums, assertions, and the `submit_manifest` passed to `submit-run.py`. |
| `cost_baseline.json` | Rolling cost baseline used by `cost_regression.evaluate`. First real run rewrites `observed_total_usd`. |
| `truvari_thresholds.json` | Standalone recall/precision thresholds for `truvari bench`, mirroring `assertions.truvari_*` in `inputs.json`. |
| `cost_regression.py` | Property-20 implementation: warn iff `observed > baseline * 1.20`, rewrite baseline only when warning fires. |
| `run_e2e.py` | Orchestration script implementing Design §Test harness step-by-step. |

## Expected outcomes

A successful run satisfies:

- HealthOmics status `COMPLETED`, wall-clock ≤ 6 h (Req 14.9).
- `<sample_id>.sv.harmonised.vcf.gz` and `.tbi` sibling present under
  `output_prefix` (Reqs 14.3, 14.5).
- Harmonised VCF header declares the `CALLERS` INFO tag (Req 14.3).
- `bcftools view -r chr20` returns ≥ 100 SV records (Req 14.4).
- `<sample_id>.run_metadata.json` reports `per_caller_status` = `succeeded`
  for PAV, Sniffles2, and PBSV (Req 14.6).
- `truvari bench` vs GIAB v0.6 chr20: recall ≥ 0.80, precision ≥ 0.80
  (Req 14.7).
- Cost regression warning fires only if `observed > baseline * 1.20`
  (Req 17.11, Property 20); when it fires, `cost_baseline.json` is
  rewritten with the new observed total. Commit the updated file when
  accepting the new baseline.

## Troubleshooting

- **`submit-run.py` exits with `RegionResidencyError`.** A bucket referenced
  in the manifest is not in `ap-southeast-1`. Move / rehost the data in
  region, or fix the URI.
- **`truvari bench` fails with `OutputDirectoryExists`.** The harness deletes
  and re-creates the directory, but a locally-clobbered state survives
  across retries; remove the workdir and re-run.
- **Cost warning fires every run.** Check whether HealthOmics has changed
  `ap-southeast-1` pricing recently (`pricing/healthomics-ap-southeast-1.json`
  is a snapshot). Refresh the price list before blaming the workflow.
