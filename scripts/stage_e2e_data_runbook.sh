#!/bin/bash
# Runbook executed on the EC2 builder to stage HG002 chr20 test data into
# the pipeline's S3 bucket, plus the GIAB v0.6 truth set and the GRCh38
# reference. Computes sha256 + size of each staged object and emits a
# summary JSON to stdout that Task 12 consumes to rewrite
# test/e2e/inputs.json locally.
#
# This runbook is the "heavy" staging path called out in the
# `stage-test-data.py` comment: it subsets the 120 GB GIAB aligned BAM
# to chr20 (produces a ~5-6 GB slice), fetches the 3.2 GB GRCh38 FASTA,
# indexes it, and downloads the 2 MB GIAB truth VCF + BED. The standard
# `stage-test-data.py` does the idempotent S3-upload half.
#
# The runbook is env-var-driven so a customer can run it against their own
# account/region/bucket without editing the file. Set ACCOUNT_ID, REGION,
# and BUCKET in the environment before invoking, or accept the marked
# placeholders below.
set -euo pipefail

REGION="${REGION:-<YOUR_REGION>}"
ACCOUNT_ID="${ACCOUNT_ID:-<YOUR_ACCOUNT>}"
BUCKET="${BUCKET:-aou-longread-sv-${ACCOUNT_ID}-${REGION}}"
WORK=/mnt/aou-sv-stage

sudo mkdir -p "$WORK"
sudo chown "$(id -u):$(id -g)" "$WORK" || true
# If /mnt/aou-sv-stage isn't writable (no secondary volume attached), fall
# back to the root volume which was provisioned at 100 GB.
if ! [ -w "$WORK" ]; then
  WORK=/tmp/aou-sv-stage
  mkdir -p "$WORK"
fi
cd "$WORK"

echo "=== step 1: ensure samtools + curl are available ==="
# AL2023 base repos do not ship samtools. Run it out of the already-mirrored
# aou-sv/hifiasm container which bakes samtools 1.16 from debian:12-slim.
SAMTOOLS_IMAGE="${SAMTOOLS_IMAGE:-${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/aou-sv/hifiasm:0.19.9-amd64}"
docker pull -q "$SAMTOOLS_IMAGE" >/dev/null
# Wrapper shim so the rest of the script can say `samtools ...` directly.
sudo tee /usr/local/bin/samtools >/dev/null <<EOF
#!/bin/bash
exec docker run --rm -u "\$(id -u):\$(id -g)" -v "$WORK:$WORK" -w "$WORK" --network host "$SAMTOOLS_IMAGE" samtools "\$@"
EOF
sudo chmod 755 /usr/local/bin/samtools
# curl ships with AL2023
command -v curl

echo "=== step 2: subset GIAB HG002 aligned BAM to chr20 ==="
# `samtools view -b -P -X` streams the slice directly from the
# public-read GIAB bucket via unsigned HTTPS — no download-whole-BAM.
# This needs samtools >= 1.13 (PR #1459). AL2023 ships 1.16.
GIAB_BAM="https://giab.s3.amazonaws.com/data/AshkenazimTrio/HG002_NA24385_son/PacBio_CCS_15kb_20kb_chemistry2/GRCh38/HG002.SequelII.merged_15kb_20kb.pbmm2.GRCh38.haplotag.10x.bam"
GIAB_BAI="https://giab.s3.amazonaws.com/data/AshkenazimTrio/HG002_NA24385_son/PacBio_CCS_15kb_20kb_chemistry2/GRCh38/HG002.SequelII.merged_15kb_20kb.pbmm2.GRCh38.haplotag.10x.bam.bai"

if [ ! -f HG002_chr20.hifi.bam ]; then
  echo "--- streaming chr20 slice ---"
  # Download the .bai into $WORK so it's visible to the docker-wrapped samtools
  curl -sSL -o giab.bam.bai "$GIAB_BAI"
  # samtools view supports remote BAM via http URL + explicit local index via -X
  samtools view -@ 8 -b -X "$GIAB_BAM" giab.bam.bai chr20 \
    -o HG002_chr20.hifi.bam
  samtools index -@ 8 HG002_chr20.hifi.bam
fi
BAM_SHA=$(sha256sum HG002_chr20.hifi.bam | cut -d' ' -f1)
BAM_SIZE=$(stat -c '%s' HG002_chr20.hifi.bam)
BAI_SHA=$(sha256sum HG002_chr20.hifi.bam.bai | cut -d' ' -f1)
BAI_SIZE=$(stat -c '%s' HG002_chr20.hifi.bam.bai)
echo "chr20 BAM: size=$BAM_SIZE sha256=$BAM_SHA"

echo "=== step 3: fetch + index GRCh38 reference ==="
if [ ! -f GRCh38.fa ]; then
  aws s3 cp --no-sign-request --region us-east-1 \
    s3://broad-references/hg38/v0/Homo_sapiens_assembly38.fasta GRCh38.fa
fi
if [ ! -f GRCh38.fa.fai ]; then
  samtools faidx GRCh38.fa
fi
REF_SHA=$(sha256sum GRCh38.fa | cut -d' ' -f1)
REF_SIZE=$(stat -c '%s' GRCh38.fa)
FAI_SHA=$(sha256sum GRCh38.fa.fai | cut -d' ' -f1)
FAI_SIZE=$(stat -c '%s' GRCh38.fa.fai)

echo "=== step 4: fetch GIAB v0.6 truth set ==="
if [ ! -f HG002_SVs_Tier1_v0.6.vcf.gz ]; then
  curl -sSL -o HG002_SVs_Tier1_v0.6.vcf.gz \
    "https://ftp-trace.ncbi.nlm.nih.gov/giab/ftp/data/AshkenazimTrio/analysis/NIST_SVs_Integration_v0.6/HG002_SVs_Tier1_v0.6.vcf.gz"
fi
if [ ! -f HG002_SVs_Tier1_v0.6.bed ]; then
  curl -sSL -o HG002_SVs_Tier1_v0.6.bed \
    "https://ftp-trace.ncbi.nlm.nih.gov/giab/ftp/data/AshkenazimTrio/analysis/NIST_SVs_Integration_v0.6/HG002_SVs_Tier1_v0.6.bed"
fi
VCF_SHA=$(sha256sum HG002_SVs_Tier1_v0.6.vcf.gz | cut -d' ' -f1)
VCF_SIZE=$(stat -c '%s' HG002_SVs_Tier1_v0.6.vcf.gz)
BED_SHA=$(sha256sum HG002_SVs_Tier1_v0.6.bed | cut -d' ' -f1)
BED_SIZE=$(stat -c '%s' HG002_SVs_Tier1_v0.6.bed)

echo "=== step 5: upload to s3://$BUCKET/test/e2e/ ==="
aws s3 cp HG002_chr20.hifi.bam     "s3://$BUCKET/test/e2e/HG002_chr20.hifi.bam"     --region "$REGION"
aws s3 cp HG002_chr20.hifi.bam.bai "s3://$BUCKET/test/e2e/HG002_chr20.hifi.bam.bai" --region "$REGION"
aws s3 cp GRCh38.fa                "s3://$BUCKET/test/e2e/GRCh38.fa"                --region "$REGION"
aws s3 cp GRCh38.fa.fai            "s3://$BUCKET/test/e2e/GRCh38.fa.fai"            --region "$REGION"
aws s3 cp HG002_SVs_Tier1_v0.6.vcf.gz "s3://$BUCKET/test/e2e/HG002_SVs_Tier1_v0.6.vcf.gz" --region "$REGION"
aws s3 cp HG002_SVs_Tier1_v0.6.bed    "s3://$BUCKET/test/e2e/HG002_SVs_Tier1_v0.6.bed"    --region "$REGION"

echo "=== summary ==="
python3 - <<PYEOF
import json
summary = {
  "hifi_reads_bam":  {"sha256":"$BAM_SHA","size_bytes":int("$BAM_SIZE")},
  "hifi_reads_bai":  {"sha256":"$BAI_SHA","size_bytes":int("$BAI_SIZE")},
  "reference_fasta": {"sha256":"$REF_SHA","size_bytes":int("$REF_SIZE")},
  "reference_fai":   {"sha256":"$FAI_SHA","size_bytes":int("$FAI_SIZE")},
  "giab_sv_vcf":     {"sha256":"$VCF_SHA","size_bytes":int("$VCF_SIZE")},
  "giab_sv_bed":     {"sha256":"$BED_SHA","size_bytes":int("$BED_SIZE")},
}
print(json.dumps(summary, indent=2))
PYEOF
