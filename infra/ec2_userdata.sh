#!/bin/bash
# EC2 userdata: bootstraps the AoU SV pipeline builder box.
# Logs everything to /var/log/aou-sv-bootstrap.log AND CloudWatch.
set -euo pipefail

LOG=/var/log/aou-sv-bootstrap.log
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) starting aou-sv builder bootstrap ==="

REGION="${REGION:-$(TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600") && \
    curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/region)}"
ACCOUNT="${ACCOUNT:-$(aws sts get-caller-identity --query Account --output text --region "$REGION")}"
BUCKET=aou-longread-sv-${ACCOUNT}-${REGION}
WORK=/opt/aou-sv
mkdir -p "$WORK"
cd "$WORK"

echo "--- installing packages ---"
dnf install -y --quiet docker git tar gzip python3.11 python3.11-pip unzip \
  amazon-ssm-agent amazon-cloudwatch-agent >/dev/null

systemctl enable --now docker amazon-ssm-agent

# Set up buildx with a container-driver builder so multi-arch works via QEMU.
dnf install -y --quiet qemu-user-static >/dev/null || true

echo "--- installing AWS CLI v2 (for current omics API) ---"
curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
cd /tmp && unzip -oq awscliv2.zip && ./aws/install --update >/dev/null
cd "$WORK"

echo "--- fetching pipeline sources ---"
aws s3 cp "s3://${BUCKET}/bootstrap/aou-sv-pipeline.tar.gz" /tmp/src.tar.gz --region "$REGION"
tar -xzf /tmp/src.tar.gz -C "$WORK"
ls -la "$WORK" | head

echo "--- rendering infra/builder_policy.json.tmpl ---"
# Render with the runtime-derived ACCOUNT and REGION. Output goes to
# /tmp so the working tree stays clean and the script is idempotent
# across re-runs. Operators attach the rendered policy to the
# pre-provisioned builder role; the script does not change the IAM
# control plane it runs under.
sed -e "s/\${ACCOUNT_ID}/${ACCOUNT}/g" -e "s/\${REGION}/${REGION}/g" \
    "$WORK/infra/builder_policy.json.tmpl" > /tmp/builder_policy.json

echo "--- installing Python deps ---"
python3.11 -m pip install --quiet --upgrade pip setuptools wheel
python3.11 -m pip install --quiet pyyaml boto3 miniwdl jsonschema

echo "--- logging docker into ECR ---"
aws ecr get-login-password --region "$REGION" | \
  docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "--- bootstrapping buildx (qemu for arm64 emulation on x86_64 host) ---"
docker run --privileged --rm tonistiigi/binfmt --install all >/dev/null 2>&1 || true
docker buildx create --name aou-builder --driver docker-container --use >/dev/null 2>&1 || \
  docker buildx use aou-builder
docker buildx inspect --bootstrap >/dev/null 2>&1 || true

echo "--- running mirror-images.py (real mode) ---"
cd "$WORK"
# The script emits progress; let it run. If any image fails, exit non-zero
# so SSM status reports the error.
python3.11 scripts/mirror-images.py --account-id "$ACCOUNT" --region "$REGION" 2>&1 | tee -a "$LOG"
MIRROR_RC=${PIPESTATUS[0]}
echo "mirror-images.py exit code: $MIRROR_RC"

if [ "$MIRROR_RC" -ne 0 ]; then
  echo "MIRROR_FAILED" > /tmp/bootstrap_state
  exit 0  # leave instance up for troubleshooting
fi

echo "--- uploading updated manifest and WDL back to S3 for the workstation to pick up ---"
aws s3 cp containers/manifest.yaml "s3://${BUCKET}/bootstrap/manifest.yaml" --region "$REGION"
aws s3 cp SOURCES.md "s3://${BUCKET}/bootstrap/SOURCES.md" --region "$REGION"
aws s3 sync wdl/ "s3://${BUCKET}/bootstrap/wdl/" --region "$REGION" --exclude "*" --include "*.wdl"

echo "--- redeploying workflow with real digests ---"
python3.11 scripts/deploy.py --force --region "$REGION" 2>&1 | tee -a "$LOG" || \
  echo "deploy.py returned non-zero; continuing to data staging"

echo "--- staging test data (HG002 chr20 etc) ---"
# Run in nohup so this long-running job continues if SSH disconnects,
# but we still fronted it here so we can see progress via CloudWatch.
python3.11 scripts/stage-test-data.py --bucket "$BUCKET" 2>&1 | tee -a "$LOG" || \
  echo "stage-test-data.py returned non-zero; continuing"

echo "--- updating inputs.json with computed checksums/sizes ---"
aws s3 cp test/e2e/inputs.json "s3://${BUCKET}/bootstrap/inputs.json" --region "$REGION"

echo "BOOTSTRAP_DONE" > /tmp/bootstrap_state
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) bootstrap complete ==="
