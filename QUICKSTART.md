# Quickstart: AoU Long-Read SV Pipeline on AWS HealthOmics

Deploy and run the full 3-caller SV detection pipeline (Hifiasm+PAV, Sniffles2, PBSV) in your own AWS account in under 1 hour.

## Prerequisites

| Requirement | Version | Check |
|---|---|---|
| AWS CLI v2 | 2.x | `aws --version` |
| Docker Desktop | with buildx | `docker buildx version` |
| Python | 3.11+ | `python3 --version` |
| AWS credentials | Admin or equivalent | `aws sts get-caller-identity` |
| Region | Any HealthOmics-supported region | `aws configure get region` |

## Step 1: Clone

```bash
git clone <this-repo-url>
cd aou-longread-sv-pipeline
```

## Step 2: Bootstrap (one command)

```bash
chmod +x scripts/bootstrap.sh
export AWS_DEFAULT_REGION=<YOUR_REGION>   # e.g. us-east-1, eu-west-1, ap-southeast-1
./scripts/bootstrap.sh --account-id <YOUR_12_DIGIT_AWS_ACCOUNT_ID>
```

This takes ~30 minutes (mostly Docker builds) and:
- Creates an S3 bucket `aou-longread-sv-<account>-<region>`
- Creates an IAM execution role `HealthOmicsAouSvExecutionRole`
- Builds and pushes 8 container images to your ECR
- Grants HealthOmics pull access to your ECR repos
- Deploys the WDL workflow to HealthOmics
- Writes `.healthomics/config.toml` with your settings

At the end it prints your **Workflow ID** and **Role ARN**.

## Step 3: Prepare your input manifest

Create a JSON file (e.g. `my_sample.json`):

```json
{
  "sample_id": "NA24385",
  "hifi_reads_bam": "s3://your-bucket/path/to/sample.hifi.bam",
  "hifi_reads_bai": "s3://your-bucket/path/to/sample.hifi.bam.bai",
  "hifi_reads_aligned": false,
  "reference_fasta": "s3://your-bucket/path/to/GRCh38.primary.fa",
  "reference_fai": "s3://your-bucket/path/to/GRCh38.primary.fa.fai",
  "output_prefix": "s3://aou-longread-sv-<account>-<region>/outputs/NA24385/",
  "run_hifiasm_pav": true,
  "run_sniffles2": true,
  "run_pbsv": true,
  "shard_by_chromosome": true,
  "run_storage_type": "DYNAMIC",
  "enable_run_cache": true,
  "input_manifest_json": "s3://your-bucket/path/to/my_sample.json"
}
```

**Important:**
- Use a **primary-only** GRCh38 reference (no ALTs/decoys). The pipeline will fail if the reference has more contigs than the BAM's @SQ headers.
- All S3 URIs must be in the same region as your HealthOmics deployment.
- Upload the manifest JSON to S3 (the `input_manifest_json` field points to itself).

## Step 4: Submit

```bash
python scripts/submit-run.py \
  --manifest my_sample.json \
  --workflow-id <WORKFLOW_ID_FROM_BOOTSTRAP> \
  --role-arn arn:aws:iam::<ACCOUNT>:role/HealthOmicsAouSvExecutionRole \
  --region <YOUR_REGION>
```

## Step 5: Monitor

```bash
# Check run status
aws omics get-run --region <YOUR_REGION> --id <RUN_ID> --query status

# List task progress
aws omics list-run-tasks --region <YOUR_REGION> --id <RUN_ID> \
  --query 'items[].[name,status]' --output table
```

## Outputs

When the run completes, outputs appear under your `output_prefix`:

```
<output_prefix>/<run_id>/out/
  harmonised_sv_vcf/          # 3-caller merged SV VCF + .tbi
  pav_sv_vcf/                 # PAV-only SV VCF + .tbi
  sniffles2_sv_vcf/           # Sniffles2 SV VCF + .tbi
  pbsv_sv_vcf/                # PBSV SV VCF + .tbi
  hifiasm_hap1_fasta/         # Haplotype-1 assembly
  hifiasm_hap2_fasta/         # Haplotype-2 assembly
  run_metadata_json/          # Per-run metadata + cost report
```

## Cost optimization

For lower cost (~26% savings, identical output), use resource overrides in your manifest:

```json
{
  "hifiasm_cpu": 16,
  "hifiasm_memory_gb": 32,
  "pav_cpu": 16,
  "pav_memory_gb": 64,
  "sniffles2_cpu": 4,
  "sniffles2_memory_gb": 16,
  "pbsv_discover_cpu": 2,
  "pbsv_discover_memory_gb": 8,
  "pbsv_call_cpu": 4,
  "pbsv_call_memory_gb": 32,
  "harmoniser_cpu": 4,
  "harmoniser_memory_gb": 16
}
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Unable to access image URI` | Run bootstrap step 4 (ECR policies) again |
| `Different number of chromosomes` | Use primary-only reference (no ALTs) |
| Task `Terminated` silently | Check memory — increase `<task>_memory_gb` |
| PAV fails at ~55 min | Ensure `--notemp` is in run_pav.sh (already default) |
| `set: Illegal option -o pipefail` | Image missing bash symlink — rebuild images |

## Architecture

```
InputValidator
  └─ Pbmm2_Align? ─┬─ Hifiasm ─ PAV ─ PAV2SVs
                    ├─ Sniffles2 (×24 shards) ─ Merge
                    └─ PBSV Discover (×24) ─ Call
                         └─ Harmoniser ─ MetadataWriter
```

All three caller branches run in parallel. Harmoniser waits for all enabled
callers, then MetadataWriter runs last.
