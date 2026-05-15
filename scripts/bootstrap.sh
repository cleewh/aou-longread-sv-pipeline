#!/bin/bash
# bootstrap.sh — One-command setup for a new AWS account.
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials (admin or equivalent)
#   - Docker with buildx (for multi-arch image builds)
#   - Python 3.11+ with pip
#   - Region: uses AWS CLI configured region (or AWS_DEFAULT_REGION env var)
#
# Usage:
#   ./scripts/bootstrap.sh --account-id <YOUR_AWS_ACCOUNT_ID>
#
# What it does:
#   1. Installs Python dependencies
#   2. Creates the IAM execution role (if absent)
#   3. Builds + pushes all 8 container images to your ECR
#   4. Sets ECR repo policies for HealthOmics
#   5. Deploys the WDL workflow to HealthOmics
#   6. Prints the workflow ID + role ARN for submit-run.py
#
# After bootstrap, run a sample with:
#   python scripts/submit-run.py \
#     --manifest <your_manifest.json> \
#     --workflow-id <printed_id> \
#     --role-arn <printed_arn> \
#     --region ap-southeast-1

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo "us-east-1")}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# --- Parse args ---
ACCOUNT_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account-id) ACCOUNT_ID="$2"; shift 2 ;;
    *) echo "Usage: $0 --account-id <AWS_ACCOUNT_ID>"; exit 1 ;;
  esac
done

if [[ -z "$ACCOUNT_ID" ]]; then
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  echo "[bootstrap] Auto-detected account: $ACCOUNT_ID"
fi

echo "============================================"
echo " AoU Long-Read SV Pipeline — Bootstrap"
echo " Account: $ACCOUNT_ID"
echo " Region:  $REGION"
echo "============================================"
echo ""

# --- 1. Python deps ---
echo "[1/6] Installing Python dependencies..."
pip install --quiet pyyaml boto3 miniwdl jsonschema 2>/dev/null || true

# --- 2. S3 bucket ---
echo "[2/7] Creating S3 bucket..."
BUCKET="aou-longread-sv-${ACCOUNT_ID}-${REGION}"
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "  Bucket $BUCKET already exists"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" \
    --query 'Location' --output text
  aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-versioning --bucket "$BUCKET" --region "$REGION" \
    --versioning-configuration Status=Enabled
  echo "  Created bucket: $BUCKET"
fi

# --- 3. IAM execution role ---
echo "[3/7] Creating IAM execution role..."
ROLE_NAME="HealthOmicsAouSvExecutionRole"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "  Role $ROLE_NAME already exists"
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document file://"${REPO_ROOT}/iam/execution_role_trust.json" \
    --description "HealthOmics execution role for AoU SV pipeline" \
    --query 'Role.Arn' --output text
  # Render and attach the inline policy
  python3 "${REPO_ROOT}/iam/render.py" \
    --template "${REPO_ROOT}/iam/execution_role_policy.json.tmpl" \
    --input-bucket "aou-longread-sv-${ACCOUNT_ID}-${REGION}" \
    --output-bucket "aou-longread-sv-${ACCOUNT_ID}-${REGION}" \
    --output-prefix "" \
    --ecr-repo-arns "arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/aou-sv/*" \
    --account-id "$ACCOUNT_ID" \
    > /tmp/aou_sv_policy.json
  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name AouSvInlinePolicy \
    --policy-document file:///tmp/aou_sv_policy.json
  echo "  Created role: $ROLE_ARN"
fi

# --- 4. Build + push container images ---
echo "[4/7] Building and pushing container images to ECR..."
cd "$REPO_ROOT"
python3 scripts/mirror-images.py --account-id "$ACCOUNT_ID" --region "$REGION"

# --- 5. ECR repo policies ---
echo "[5/7] Setting ECR repository policies for HealthOmics..."
POLICY=$(cat "${REPO_ROOT}/iam/ecr_repo_policy.json")
for repo in aou-sv/hifiasm aou-sv/pbmm2 aou-sv/sniffles2 aou-sv/pbsv aou-sv/pav aou-sv/pav2svs aou-sv/harmoniser aou-sv/metadata-writer; do
  aws ecr set-repository-policy --region "$REGION" \
    --repository-name "$repo" \
    --policy-text "$POLICY" \
    --query 'repositoryName' --output text 2>/dev/null || true
done
echo "  Done"

# --- 6. Stamp WDL digests + deploy workflow ---
echo "[6/7] Stamping WDL with ECR digests and deploying workflow..."
python3 scripts/stamp-wdl-digests.py
python3 scripts/deploy.py --force --region "$REGION"

# --- 7. Write config ---
echo "[7/7] Writing .healthomics/config.toml..."
mkdir -p "${REPO_ROOT}/.healthomics"
cat > "${REPO_ROOT}/.healthomics/config.toml" <<EOF
account_id = "${ACCOUNT_ID}"
region = "${REGION}"
bucket = "${BUCKET}"
role_arn = "${ROLE_ARN}"
EOF

# --- Print summary ---
WORKFLOW_ID=$(aws omics list-workflows --region "$REGION" --name "aou-longread-sv-pipeline" \
  --query 'items[0].id' --output text 2>/dev/null || echo "unknown")

echo ""
echo "============================================"
echo " Bootstrap complete!"
echo "============================================"
echo ""
echo " Workflow ID:  $WORKFLOW_ID"
echo " Role ARN:     $ROLE_ARN"
echo " Region:       $REGION"
echo ""
echo " To run a sample:"
echo ""
echo "   python scripts/submit-run.py \\"
echo "     --manifest <your_manifest.json> \\"
echo "     --workflow-id $WORKFLOW_ID \\"
echo "     --role-arn $ROLE_ARN \\"
echo "     --region $REGION"
echo ""
echo " See README.md for manifest format and examples."
