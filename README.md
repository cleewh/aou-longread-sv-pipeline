# AoU Long-Read SV Detection Pipeline on AWS HealthOmics

This repository ports the Broad Institute All of Us (AoU) Phase 1
long-read structural-variant (SV) detection stack to AWS HealthOmics.
Input is a single PacBio HiFi sample (unaligned or aligned BAM). Output
is a harmonised SV VCF plus per-caller VCFs produced by the three
complementary callers described in the AoU paper (Mahmoud et al. 2023,
*"Utility of long-read sequencing for All of Us"*, bioRxiv
2023.01.23.525236):

- **Assembly-based:** Hifiasm → PAV → PAV2SVs filter.
- **Read-based:** Sniffles2.
- **Read-based:** PBSV.

Per-sample callsets from the three callers are merged and filtered by
the `callset_integration_phase2` harmoniser into a single reconciled
SV VCF with a `CALLERS` INFO tag that records which caller(s) supported
each variant.

Version: see [`VERSION`](./VERSION). License: BSD-3-Clause (see
`pyproject.toml`).

## Two ways to consume this project

### A. Just need a container image (e.g. running PAV alone)

Pre-built images are published to GitHub Container Registry under
`ghcr.io/cleewh/aou-sv/<tool>`. They're public — no `docker login` or
AWS credentials needed. Pull with:

```bash
docker pull ghcr.io/cleewh/aou-sv/pav:2.4.6
docker pull ghcr.io/cleewh/aou-sv/hifiasm:0.19.9
docker pull ghcr.io/cleewh/aou-sv/pbmm2:1.13.1
docker pull ghcr.io/cleewh/aou-sv/sniffles2:2.4
docker pull ghcr.io/cleewh/aou-sv/pbsv:2.9.0
docker pull ghcr.io/cleewh/aou-sv/pav2svs:0.1.0
docker pull ghcr.io/cleewh/aou-sv/harmoniser:0.1.0
docker pull ghcr.io/cleewh/aou-sv/metadata-writer:0.1.0
```

No clone needed for this path.

### B. Running the full SV-detection pipeline on AWS HealthOmics

You need to clone this repo and run the bootstrap. AWS HealthOmics can
only pull from your own ECR, so the bootstrap mirrors the GHCR images
into your account's ECR and registers the WDL workflow:

```bash
git clone https://github.com/cleewh/aou-longread-sv-pipeline.git
cd aou-longread-sv-pipeline
./scripts/bootstrap.sh --account-id <YOUR_AWS_ACCOUNT_ID>
```

Bootstrap pulls the images **from GHCR by default** (fast, no Docker
Hub rate limits). To force a fresh build from upstream, pass:

```bash
python3 scripts/mirror-images.py --account-id <YOUR_ID> --source upstream
```

Then submit a run:

```bash
python3 scripts/submit-run.py \
    --manifest path/to/my_manifest.json \
    --workflow-id <workflow_id_from_bootstrap> \
    --role-arn <role_arn_from_bootstrap>
```

Full quickstart with prerequisites is in the **Quick start** section
below.

The publish workflow lives at
[`.github/workflows/publish-images.yml`](./.github/workflows/publish-images.yml)
and can be re-triggered manually from the repo's **Actions** tab.

## Region and data residency

The pipeline is region-agnostic — it uses your AWS CLI configured region
by default. All scripts (`bootstrap.sh`, `deploy.py`, `submit-run.py`,
`mirror-images.py`) resolve the region from your environment
(`AWS_DEFAULT_REGION`, `aws configure get region`, or `--region` flag).

The residency checks in `submit-run.py` validate that all S3 buckets and
ECR image URIs are in the same region as the target deployment. This
ensures data never crosses region boundaries during a run.

To deploy in a specific region:

```bash
export AWS_DEFAULT_REGION=ap-southeast-1  # or us-east-1, eu-west-1, etc.
./scripts/bootstrap.sh --account-id <YOUR_ACCOUNT_ID>
```

## Quick start

Deploy and run the full 3-caller SV detection pipeline (Hifiasm+PAV,
Sniffles2, PBSV) in your own AWS account. End-to-end takes about
30 minutes for bootstrap plus ~2 hours for an HG002 chr20 run (~$5),
or ~17 hours for a whole-genome 30× HiFi sample (~$81).

### 1. Prerequisites

| Requirement | Version | Check |
|---|---|---|
| AWS CLI v2 | 2.x | `aws --version` |
| Docker Desktop | with `buildx` | `docker buildx version` |
| Python | 3.11+ | `python3 --version` |
| AWS credentials | Admin or equivalent | `aws sts get-caller-identity` |
| Region | Any HealthOmics-supported region | `aws configure get region` |

### 2. Clone

```bash
git clone https://github.com/cleewh/aou-longread-sv-pipeline.git
cd aou-longread-sv-pipeline
```

### 3. Bootstrap (one command)

```bash
chmod +x scripts/bootstrap.sh
export AWS_DEFAULT_REGION=<YOUR_REGION>   # e.g. us-east-1, eu-west-1, ap-southeast-1
./scripts/bootstrap.sh --account-id <YOUR_12_DIGIT_AWS_ACCOUNT_ID>
```

This takes ~30 minutes and:

- Creates the S3 bucket `aou-longread-sv-<account>-<region>`
- Creates the IAM execution role `HealthOmicsAouSvExecutionRole`
- Pulls 8 pre-built images from `ghcr.io/cleewh/aou-sv/*` and pushes
  them into your ECR (no Docker Hub credentials needed; pass
  `--source upstream` if you want to rebuild from upstream Dockerfiles
  instead)
- Grants HealthOmics pull access to your ECR repos
- Stamps WDL `runtime.docker` references with your ECR digests
- Deploys the WDL workflow to HealthOmics
- Writes `.healthomics/config.toml` with your settings

At the end it prints your **Workflow ID** and **Role ARN** — record
these.

### 4. Stage test data (optional, for the HG002 chr20 e2e smoke test)

To run the HG002 chr20 smoke test, stage GIAB's public HG002 chr20
BAM, the GRCh38 reference, and the GIAB v0.6 truth set into your
bucket:

```bash
python3 scripts/stage-test-data.py \
    --bucket aou-longread-sv-<YOUR_ACCOUNT>-<YOUR_REGION>
```

Skip this if you're running on your own data.

### 5. Render a submit manifest

The sample manifests in `test/e2e/` and `test/wgs/` use `<YOUR_BUCKET>`
and `<YOUR_ACCOUNT>` placeholders. Substitute and upload to S3:

```bash
sed \
    -e "s|<YOUR_BUCKET>|aou-longread-sv-<YOUR_ACCOUNT>-<YOUR_REGION>|g" \
    -e "s|<YOUR_ACCOUNT>|<YOUR_ACCOUNT>|g" \
    test/e2e/submit_manifest.json > my_manifest.json

aws s3 cp my_manifest.json \
    s3://aou-longread-sv-<YOUR_ACCOUNT>-<YOUR_REGION>/test/e2e/submit_manifest.json
```

The `input_manifest_json` field points to the manifest's own S3
location, so it must exist there before `submit-run.py` runs.

### 6. Submit

```bash
python3 scripts/submit-run.py \
    --manifest my_manifest.json \
    --workflow-id <WORKFLOW_ID_FROM_BOOTSTRAP> \
    --role-arn arn:aws:iam::<YOUR_ACCOUNT>:role/HealthOmicsAouSvExecutionRole \
    --region <YOUR_REGION>
```

`submit-run.py` runs pre-flight checks: residency validation,
Input_Manifest schema validation, resource override resolution,
chromosome shard planning, and cost-optimal instance selection.
`--dry-run` prints every check and exits without calling `StartRun`.

The script prints the run ID on success.

### 7. Monitor

```bash
# Run status
aws omics get-run --region <YOUR_REGION> --id <RUN_ID> --query status \
    --output text

# Per-task progress
aws omics list-run-tasks --region <YOUR_REGION> --id <RUN_ID> \
    --query 'items[].[name,status]' --output table
```

When the run completes, outputs land under your `output_prefix`. See
[Output layout](#output-layout) below.

### Sample manifests

| File | Use case |
|---|---|
| `test/e2e/submit_manifest.json` | HG002 chr20 smoke test, default sizing (~2h, ~$5) |
| `test/e2e/submit_manifest_optimised.json` | HG002 chr20, right-sized resources (~26% cheaper) |
| `test/e2e/pav_only_manifest.json` | Run only the PAV branch on pre-assembled haplotypes |
| `test/wgs/submit_manifest_wgs_optimised.json` | Whole-genome 30× HiFi, optimised sizing (~17h, ~$81) |

### Architecture

```
InputValidator
  └─ Pbmm2_Align? ─┬─ Hifiasm ─ PAV ─ PAV2SVs
                    ├─ Sniffles2 (×24 shards) ─ Merge
                    └─ PBSV Discover (×24) ─ Call
                         └─ Harmoniser ─ MetadataWriter
```

All three caller branches run in parallel. Harmoniser waits for all
enabled callers, then MetadataWriter runs last.

### Troubleshooting

| Issue | Fix |
|---|---|
| `Unable to access image URI` | Re-run bootstrap step 5 (ECR repo policies); or check that `mirror-images.py` finished successfully |
| `Different number of chromosomes` | Use a primary-only GRCh38 reference (no ALTs/decoys) |
| Task `Terminated` silently | Check task memory; bump `<task>_memory_gb` in the manifest |
| `RegionResidencyError` from `submit-run.py` | An S3 URI in the manifest is not in your deployment region |
| `Unexpected workflow parameters` from StartRun | A field in your manifest is not declared in `wdl/parameter_template.json`; remove it |

## Benchmarks (GIAB HG002, 30× HiFi, ap-southeast-1)

Tested on GIAB HG002 PacBio CCS 15kb+20kb merged BAM (~36× effective
coverage, 111.6 GiB) from the public Genome in a Bottle consortium
bucket.

### Recommended whole-genome configuration

| Task | vCPUs | Memory | Instance |
|------|-------|--------|----------|
| Hifiasm | 64 | 256 GB | omics.r.16xlarge |
| PAV | 32 | 128 GB | omics.m.8xlarge |
| Sniffles2 (per shard) | 8 | 32 GB | omics.m.2xlarge |
| PBSV discover (per shard) | 4 | 16 GB | omics.m.xlarge |
| PBSV call | 8 | 64 GB | omics.r.2xlarge |
| Harmoniser | 8 | 32 GB | omics.m.2xlarge |

Hifiasm at 64 vCPU is the optimal configuration — benchmarking showed
no meaningful improvement at 96 vCPU (9.3h vs 9.1h) due to serial
bottlenecks in the assembly algorithm. PAV performs best at 32 vCPU —
higher core counts do not improve throughput due to I/O bottlenecks.

**Estimated end-to-end runtime:** ~17 hours per sample.
**Estimated cost:** ~$81 per sample (compute + dynamic storage).

See `test/wgs/submit_manifest_wgs_optimised.json` for the full manifest.

### Cost-saving option

Set `run_hifiasm_pav: false` to skip the assembly-based caller. This
reduces cost by ~70% (hifiasm + PAV dominate the bill) at the expense
of losing ~400 SVs per sample that only assembly-based calling detects.

## Submit via `aws omics start-run` directly

The pipeline is also submittable via the AWS CLI directly:

```bash
aws omics start-run \
    --workflow-id <workflow_id> \
    --role-arn <role_arn> \
    --name aou-sv-<sample_id> \
    --parameters file://my_manifest.json \
    --storage-type DYNAMIC
```

Direct invocations bypass the client-side residency gate. Operators who
take this path are responsible for confirming that every S3 URI and ECR
URI referenced by their manifest is in the same region as the workflow.

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
| `hifiasm_bloom_filter_bits` | `37` | Hifiasm bloom filter size exponent. Use `37` for whole-genome (16 GB bloom table, faster). Use `0` for chr20/small inputs (disables bloom filter, lower memory). |
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

The pipeline targets minimum HealthOmics spend:

- **DYNAMIC run storage** — billed per GB-hour actually consumed.
- **Cost-optimal instance selection** — each task is fulfilled by the
  smallest instance that satisfies the cpu/memory/disk request.
- **Graviton images where upstream supports arm64** (hifiasm, pbmm2,
  sniffles2, pav2svs, harmoniser, metadata-writer).
- **Chromosome sharding** of Sniffles2 and PBSV for parallelism.
- **Skip-alignment short-circuit** when `hifi_reads_aligned=true`.

Every run writes a `Cost_Report` block into `<sample_id>.run_metadata.json`
with per-task instance type, CPU-hours, memory-GB-hours,
storage-GB-hours, and estimated USD.

### Resource overrides

Supply per-task overrides via the `<task>_cpu` / `<task>_memory_gb` /
`<task>_disk_gb` Input_Manifest fields. Any field left unset falls back
to the task default declared in `wdl/parameter_template.json`; a partial
override preserves the other defaults per Property 11. The resolved
values flow through `scripts/submit_run/resources.py` before `StartRun`.

## Optional: Budget_Alarm

The pipeline ships an optional AWS Budgets + SNS alarm as a
CloudFormation template (`scripts/budget-alarm.yaml`). It creates a
monthly HealthOmics spend budget in your deployment region and publishes
an alarm to the SNS topic you supply when actual or forecast spend
breaches the threshold (Req 17.13).

Deploy it alongside the workflow:

```bash
python3 scripts/deploy.py --with-budget-alarm \
    --budget-threshold-usd 500 \
    --budget-sns-topic-arn arn:aws:sns:<YOUR_REGION>:<YOUR_ACCOUNT>:cost-alerts
```

The core pipeline is deployable without this flag.

## Limitations

- **PacBio HiFi only.** Oxford Nanopore (ONT) and PacBio CLR reads are
  not supported. Non-HiFi BAMs fail at the Hifiasm header check.
- **Single-sample calling only.** No joint genotyping, no cohort merge.
  Phase 2 cohort workflows are out of scope.
- **GRCh38 only.** Built and tested against GRCh38 primary (no
  ALTs/decoys). GRCh37 and T2T are not supported.
- **Whole-genome sizing.** The default `hifiasm_bloom_filter_bits=37`
  is sized for whole-genome 30× HiFi. For chr20 or subset inputs, set
  `hifiasm_bloom_filter_bits=0` and reduce memory/disk accordingly
  (see `test/e2e/submit_manifest_optimised.json` for chr20 sizing).
- **Max instance size varies by region.** Singapore (`ap-southeast-1`)
  supports up to 96 vCPU (24xlarge). US regions (`us-east-1`,
  `us-west-2`) support up to 192 vCPU (48xlarge). Check
  [HealthOmics service quotas](https://docs.aws.amazon.com/omics/latest/dev/service-quotas.html)
  for your chosen region.

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
