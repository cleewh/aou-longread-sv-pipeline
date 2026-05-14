# AoU Long-Read SV Detection Pipeline on AWS HealthOmics

This repository ports the Broad Institute All of Us (AoU) Phase 1
long-read structural-variant (SV) detection stack to AWS HealthOmics in
the Singapore region (`ap-southeast-1`). Input is a single PacBio HiFi
sample (unaligned or aligned BAM). Output is a harmonised SV VCF plus
per-caller VCFs produced by the three complementary callers described
in the AoU paper (Mahmoud et al. 2023, *"Utility of long-read sequencing
for All of Us"*, bioRxiv 2023.01.23.525236):

- **Assembly-based:** Hifiasm → PAV → PAV2SVs filter.
- **Read-based:** Sniffles2.
- **Read-based:** PBSV.

Per-sample callsets from the three callers are merged and filtered by
the `callset_integration_phase2` harmoniser into a single reconciled
SV VCF with a `CALLERS` INFO tag that records which caller(s) supported
each variant.

Version: see [`VERSION`](./VERSION). License: BSD-3-Clause (see
`pyproject.toml`).

## Region and data residency

This deployment is pinned to `ap-southeast-1` (Singapore) per
[Requirement 10](./.kiro/specs/aou-longread-sv-pipeline/requirements.md).
Concretely:

- The HealthOmics workflow resource is created in `ap-southeast-1` by
  `scripts/deploy.py`.
- `scripts/submit-run.py` fails submission with `RegionResidencyError`
  when any S3 URI in the Input_Manifest resolves to a bucket outside
  `ap-southeast-1`, and fails with `EcrResidencyError` when any
  referenced ECR image URI is not in the `ap-southeast-1` registry.
- Container images are mirrored into the `ap-southeast-1` ECR by
  `scripts/mirror-images.py`; the WDL never pulls from a cross-region
  registry.

Operators running in a different region must fork the pipeline,
refresh `pricing/healthomics-ap-southeast-1.json` with the target
region's rate card, and update the `ap-southeast-1` checks in
`scripts/submit_run/residency.py`. No cross-region deployment is
supported out of the box.

## Quick start

### 1. Prerequisites

- An AWS account in `ap-southeast-1` with AWS HealthOmics enabled.
- An S3 bucket in `ap-southeast-1` to hold inputs and outputs.
- An IAM execution role for HealthOmics, rendered from
  [`iam/execution_role_trust.json`](./iam/execution_role_trust.json) +
  [`iam/execution_role_policy.json.tmpl`](./iam/execution_role_policy.json.tmpl)
  via [`iam/render.py`](./iam/render.py). Capture its ARN.
- Local tools: Python 3.11+, Docker with `buildx`, `miniwdl`, `boto3`,
  `jsonschema`, `PyYAML`, `pytest`, `hypothesis`, `cfn-lint`.
  `pip install -e '.[dev]'` installs every dev dependency.

### 2. Stage the test dataset

`scripts/stage-test-data.py` walks `test/e2e/inputs.json` and uploads
the HG002 chr20 HiFi BAM, the GRCh38 no-alt reference, and the GIAB
v0.6 Tier-1 SV truth set to the target bucket. It skips objects already
present with matching size + SHA-256, so the command is idempotent.

```bash
python3 scripts/stage-test-data.py --bucket aou-longread-sv-<account-id>-ap-southeast-1
```

### 3. Build and push container images

```bash
python3 scripts/mirror-images.py --account-id <your-account-id>
```

Multi-arch images (Graviton + x86_64 where upstream supports arm64)
are pushed to `ap-southeast-1` ECR and their per-platform digests are
written back to `containers/manifest.yaml` and appended to the
`## Image digests` section of [`SOURCES.md`](./SOURCES.md).

### 4. Deploy the workflow

```bash
python3 scripts/deploy.py --region ap-southeast-1
```

This runs `miniwdl check wdl/main.wdl`, zips `wdl/`, and calls
`aws omics create-workflow` (or `update-workflow` with `--force`) in
`ap-southeast-1`. The workflow ID (`wfl-xxxxxxxx`) is printed on
success.

### 5. Submit a run

```bash
python3 scripts/submit-run.py \
    --manifest path/to/my_manifest.json \
    --workflow-id wfl-xxxxxxxx \
    --role-arn arn:aws:iam::<account-id>:role/AouLongReadSvExecutionRole \
    --region ap-southeast-1
```

`submit-run.py` composes the full pre-flight pipeline: residency
checks, Input_Manifest schema validation, resource override resolution
(Property 11), chromosome shard planning (Property 18), and cost-optimal
instance selection (Property 15). `--dry-run` prints every check and
exits without calling `StartRun`.

### 6. Run the end-to-end smoke test

```bash
python3 test/e2e/run_e2e.py \
    --bucket aou-longread-sv-<account-id>-ap-southeast-1 \
    --workflow-id wfl-xxxxxxxx \
    --role-arn arn:aws:iam::<account-id>:role/AouLongReadSvExecutionRole
```

`run_e2e.py` submits the HG002 chr20 smoke run, polls until terminal
state (failing with `WallClockExceeded` at 6 h per Req 14.9), checks
output layout, SV record counts, per-caller status, and Truvari recall
and precision against GIAB v0.6. Detailed procedure:
[`test/e2e/README.md`](./test/e2e/README.md).

## Submit via `aws omics start-run` directly

Per Requirement 13.4, the pipeline is also submittable via the AWS CLI
directly — useful for smoke-testing the workflow definition without the
Python gating in `submit-run.py`. The exact command is:

```bash
aws omics start-run \
    --workflow-id wfl-xxxxxxxx \
    --role-arn arn:aws:iam::<account-id>:role/AouLongReadSvExecutionRole \
    --name aou-sv-HG002-chr20 \
    --parameters file://my_manifest.json \
    --storage-type DYNAMIC \
    --region ap-southeast-1
```

Direct `aws omics start-run` invocations bypass the client-side
region-residency gate (Design D10). Operators who take this path are
responsible for confirming that every S3 URI and ECR URI referenced by
their manifest is in `ap-southeast-1`.

## Input_Manifest reference

The Input_Manifest is a JSON document passed as
`--parameters file://<path>` to `aws omics start-run` (or via
`submit-run.py --manifest <path>`). Every field below maps to a top-level
input in [`wdl/main.wdl`](./wdl/main.wdl); full per-field descriptions
and defaults live in
[`wdl/parameter_template.json`](./wdl/parameter_template.json).

### Required fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `sample_id` | string | Matches `^[A-Za-z0-9_-]+$`; prefixes every output basename (Req 2.1, 7.3). |
| `hifi_reads_bam` | S3 URI | PacBio HiFi BAM (aligned or unaligned). |
| `reference_fasta` | S3 URI | GRCh38 no-alt FASTA (or equivalent). |
| `reference_fai` | S3 URI | FAI index of `reference_fasta`. |
| `output_prefix` | S3 URI | S3 prefix ending in `/` under which the Output_Bundle is written (Req 7.1, 7.4). |
| `input_manifest_json` | S3 URI | Serialised Input_Manifest, echoed into `run_metadata.json` by MetadataWriter (Req 16.2). `submit-run.py` writes this automatically before `StartRun`. |

### Optional fields

| Field | Default | Description |
| ----- | ------- | ----------- |
| `hifi_reads_bai` | null | BAM index; required when `hifi_reads_aligned=true` (Req 2.3). |
| `hifi_reads_aligned` | `false` | Skip `Pbmm2_Align` when `true` (Req 17.9). |
| `run_hifiasm_pav` | `true` | Enable Hifiasm → PAV → PAV2SVs chain (Req 2.6). |
| `run_sniffles2` | `true` | Enable Sniffles2 (Req 2.6). |
| `run_pbsv` | `true` | Enable PBSV (Req 2.6). |
| `shard_by_chromosome` | `true` | Fan Sniffles2 and PBSV out per chromosome (Design D7, Req 17.8). |
| `run_storage_type` | `"DYNAMIC"` | `DYNAMIC` or `STATIC` (Design D6, Req 17.2). |
| `enable_run_cache` | `true` | Use HealthOmics call-cache when available (Design D16, Req 17.12). |
| `harmoniser_filter_override_json` | null | S3 URI to a JSON with harmoniser threshold overrides (Req 6.6). |
| `<task>_cpu` / `<task>_memory_gb` / `<task>_disk_gb` | null | Per-task resource overrides (Req 11.2, 11.3, Property 11). |
| `pipeline_version` | `"0.1.0"` | Must match `VERSION` and `meta.version` in `wdl/main.wdl` (Property 14, Req 16.3). |
| `git_commit` | `"unknown"` | Git SHA recorded in `run_metadata.json` + the workflow-start log line (Req 12.3). |

## Output layout

Everything under `output_prefix` carries the `sample_id` prefix (Req
7.3, Property 4). Conditional outputs are present only when their
producing branch was enabled and succeeded.

| File | Always present? | Description |
| ---- | --------------- | ----------- |
| `<sample_id>.sv.harmonised.vcf.gz` + `.tbi` | yes | Final reconciled SV VCF with `CALLERS` INFO tag (Req 6, 7.1). |
| `<sample_id>.sv.pav.vcf.gz` + `.tbi` | when `run_hifiasm_pav=true` | PAV2SVs per-caller VCF. |
| `<sample_id>.sv.sniffles2.vcf.gz` + `.tbi` | when `run_sniffles2=true` | Sniffles2 per-caller VCF. |
| `<sample_id>.sv.pbsv.vcf.gz` + `.tbi` | when `run_pbsv=true` | PBSV per-caller VCF. |
| `<sample_id>.hap1.fa.gz` / `<sample_id>.hap2.fa.gz` | when `run_hifiasm_pav=true` | Hifiasm haplotype-resolved assemblies. |
| `<sample_id>.run_metadata.json` | yes | Run identifiers, tool versions, image digests, per-caller status, `Cost_Report` (Req 7.2, 12.2, 16.2, 17.10). |

HealthOmics-managed CloudWatch log streams capture each task's stdout
and stderr per Req 12.1.

## Cost model

The pipeline targets minimum HealthOmics spend by default per
Requirement 17:

- **DYNAMIC run storage** is the default (`run_storage_type`). Storage
  is billed per GB-hour actually consumed; large intermediates are
  reclaimed as soon as a downstream task finishes (Design D6).
- **Cost-optimal instance selection** (Property 15): each task's
  declared `cpu` / `memory_gb` / `disk_gb` request is fulfilled by the
  smallest `ap-southeast-1` HealthOmics instance family that satisfies
  all three bounds, using the embedded price list
  [`pricing/healthomics-ap-southeast-1.json`](./pricing/healthomics-ap-southeast-1.json)
  (Design D9).
- **Graviton images where upstream supports arm64** (hifiasm, pbmm2,
  sniffles2, pav2svs, harmoniser, metadata-writer). PAV and PBSV are
  pinned to amd64 because their upstream vendor builds are amd64-only;
  see the `## Graviton matrix` table in [`SOURCES.md`](./SOURCES.md).
- **Chromosome sharding** of Sniffles2 and PBSV when
  `shard_by_chromosome=true` (Design D7, Req 17.8).
- **Skip-alignment short-circuit** when `hifi_reads_aligned=true`
  (Req 17.9).
- **Disabled non-essential options** — see the `## Disabled non-essential
  options` section in [`SOURCES.md`](./SOURCES.md) for the full list
  (Req 17.14).

Every run writes a `Cost_Report` block into `<sample_id>.run_metadata.json`
with per-task instance type, CPU-hours, memory-GB-hours,
storage-GB-hours, and estimated USD, plus a run-level total (Req 17.10,
Property 19). `test/e2e/cost_regression.py` warns when the total exceeds
the committed baseline by more than 20 % and rewrites
`test/e2e/cost_baseline.json` on warning (Req 17.11, Property 20).

### Resource overrides

Supply per-task overrides via the `<task>_cpu` / `<task>_memory_gb` /
`<task>_disk_gb` Input_Manifest fields. Any field left unset falls back
to the task default declared in `wdl/parameter_template.json`; a partial
override preserves the other defaults per Property 11. The resolved
values flow through `scripts/submit_run/resources.py` before `StartRun`.

## Optional: Budget_Alarm

The pipeline ships an optional AWS Budgets + SNS alarm as a
CloudFormation template (`scripts/budget-alarm.yaml`). It creates a
monthly HealthOmics spend budget in `ap-southeast-1` and publishes an
alarm to the SNS topic you supply when actual or forecast spend breaches
the threshold (Req 17.13).

Deploy it alongside the workflow:

```bash
python3 scripts/deploy.py --with-budget-alarm \
    --budget-threshold-usd 500 \
    --budget-sns-topic-arn arn:aws:sns:ap-southeast-1:<account-id>:cost-alerts
```

The core pipeline is deployable without this flag.

## Limitations

- **PacBio HiFi only.** Oxford Nanopore (ONT) and PacBio CLR reads are
  not supported; all three callers and the Hifiasm assembler are tuned
  for HiFi. Non-HiFi BAMs fail at the Hifiasm or Sniffles2 header check.
- **Single-sample calling only.** No joint genotyping, no cohort merge.
  Phase 2 cohort workflows are explicitly out of scope for this repo.
- **`ap-southeast-1` only.** Other regions require a fork that refreshes
  the embedded price list, the residency gate, and the ECR registry
  host; no cross-region deployment is supported out of the box.
- **Whole-genome resource defaults are NOT yet validated on a real 30×
  human sample.** The values in `wdl/parameter_template.json` today are
  AoU Phase 1 whole-genome estimates. Task 22's HG002 chr20 E2E run
  calibrates chr20-sized defaults with 25 % headroom per Req 17.4; the
  observed high-water marks land in the `## Measured high-water marks`
  section of [`SOURCES.md`](./SOURCES.md). Until that pass completes
  on a whole-genome sample, operators running whole-genome samples
  MUST supply overrides via `resource_overrides` in the Input_Manifest.
  See Design D12 for the detailed rationale.

## References

- [`SOURCES.md`](./SOURCES.md) — upstream commits, image digests,
  Graviton matrix, pricing source, disabled options, WDL adaptation
  notes.
- [`test/e2e/README.md`](./test/e2e/README.md) — end-to-end smoke-test
  procedure with Truvari and cost-regression steps.
- Mahmoud, M. et al. (2023). "Utility of long-read sequencing for All
  of Us." bioRxiv 2023.01.23.525236.
- Design document: `.kiro/specs/aou-longread-sv-pipeline/design.md`.
- Requirements: `.kiro/specs/aou-longread-sv-pipeline/requirements.md`.

## License

BSD-3-Clause. See `pyproject.toml`.
