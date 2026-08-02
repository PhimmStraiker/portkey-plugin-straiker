#!/usr/bin/env bash
# Build the coding-agent middleware image, push to ECR, and deploy to App Runner.
#
# The Straiker key goes to Secrets Manager and is injected as a RuntimeEnvironmentSecret.
# It is never written to this script, to the image, or to the service's plaintext env.
#
# Prereqs: refreshed AWS creds, docker running, and the key exported:
#     export STRAIKER_API_KEY=...      # coding-agent app key
#     ./deploy/deploy.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

REGION="${AWS_REGION:-us-east-2}"
IMG="straiker-portkey-coding-middleware"
SVC="straiker-portkey-coding-middleware"
SECRET_NAME="${SECRET_NAME:-straiker/portkey-coding/api-key}"
DETECT_URL="${STRAIKER_DETECT_URL:-https://api.prod.straiker.ai/api/v1/detect}"

: "${STRAIKER_API_KEY:?export STRAIKER_API_KEY before running (it is not stored in this repo)}"

ACCT="$(aws sts get-caller-identity --query Account --output text)"
ECR="$ACCT.dkr.ecr.$REGION.amazonaws.com"
TAG="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo latest)"

echo "== region $REGION / account $ACCT / tag $TAG =="

echo "== 1. secret -> Secrets Manager =="
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
    --secret-string "$STRAIKER_API_KEY" --region "$REGION" >/dev/null
else
  aws secretsmanager create-secret --name "$SECRET_NAME" \
    --description "Straiker coding-agent app key used by the Portkey middleware" \
    --secret-string "$STRAIKER_API_KEY" --region "$REGION" >/dev/null
fi
SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" --query ARN --output text)"
echo "   $SECRET_ARN"

echo "== 2. ECR repo + login =="
aws ecr describe-repositories --repository-names "$IMG" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$IMG" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"

echo "== 3. build + push =="
docker build --platform linux/amd64 -f "$REPO/middleware/Dockerfile" \
  -t "$ECR/$IMG:$TAG" -t "$ECR/$IMG:latest" "$REPO/middleware"
docker push -q "$ECR/$IMG:$TAG"
docker push -q "$ECR/$IMG:latest"

echo "== 4. IAM roles =="
ACCESS_ROLE_ARN="$(aws iam get-role --role-name AppRunnerECRAccessRole --query Role.Arn --output text 2>/dev/null || echo "")"
if [ -z "$ACCESS_ROLE_ARN" ]; then
  echo "   creating AppRunnerECRAccessRole"
  ACCESS_ROLE_ARN="$(aws iam create-role --role-name AppRunnerECRAccessRole \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --query Role.Arn --output text)"
  aws iam attach-role-policy --role-name AppRunnerECRAccessRole \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
  sleep 10
fi

INSTANCE_ROLE_NAME="straiker-portkey-coding-instance-role"
INSTANCE_ROLE_ARN="$(aws iam get-role --role-name "$INSTANCE_ROLE_NAME" --query Role.Arn --output text 2>/dev/null || echo "")"
if [ -z "$INSTANCE_ROLE_ARN" ]; then
  echo "   creating $INSTANCE_ROLE_NAME"
  INSTANCE_ROLE_ARN="$(aws iam create-role --role-name "$INSTANCE_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"tasks.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --query Role.Arn --output text)"
  sleep 10
fi
aws iam put-role-policy --role-name "$INSTANCE_ROLE_NAME" --policy-name read-straiker-key \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"$SECRET_ARN\"}]}"
echo "   access=$ACCESS_ROLE_ARN"
echo "   instance=$INSTANCE_ROLE_ARN"

echo "== 5. create/update App Runner service =="
ENV_JSON=$(cat <<EOF
{ "STRAIKER_DETECT_URL":"$DETECT_URL",
  "STRAIKER_BLOCK_ENABLED":"${STRAIKER_BLOCK_ENABLED:-true}",
  "STRAIKER_CHATTER_FILTER":"true",
  "STRAIKER_DETECT_TIMEOUT":"${STRAIKER_DETECT_TIMEOUT:-2.5}",
  "STRAIKER_DEFAULT_USER_NAME":"${STRAIKER_DEFAULT_USER_NAME:-portkey-coding}" }
EOF
)
SRC=$(cat <<EOF
{ "ImageRepository": {
    "ImageIdentifier":"$ECR/$IMG:$TAG","ImageRepositoryType":"ECR",
    "ImageConfiguration":{ "Port":"8080",
      "RuntimeEnvironmentVariables":$ENV_JSON,
      "RuntimeEnvironmentSecrets":{"STRAIKER_API_KEY":"$SECRET_ARN"} } },
  "AutoDeploymentsEnabled": false,
  "AuthenticationConfiguration": { "AccessRoleArn":"$ACCESS_ROLE_ARN" } }
EOF
)
HEALTH='{"Protocol":"HTTP","Path":"/health","Interval":10,"Timeout":5,"HealthyThreshold":1,"UnhealthyThreshold":5}'
INSTANCE="{\"Cpu\":\"1024\",\"Memory\":\"2048\",\"InstanceRoleArn\":\"$INSTANCE_ROLE_ARN\"}"

ARN="$(aws apprunner list-services --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='$SVC'].ServiceArn" --output text)"
if [ -n "$ARN" ]; then
  aws apprunner update-service --service-arn "$ARN" --source-configuration "$SRC" \
    --instance-configuration "$INSTANCE" --region "$REGION" >/dev/null
  echo "   updated $ARN"
else
  ARN="$(aws apprunner create-service --service-name "$SVC" --source-configuration "$SRC" \
    --instance-configuration "$INSTANCE" --health-check-configuration "$HEALTH" \
    --region "$REGION" --query Service.ServiceArn --output text)"
  echo "   created $ARN"
fi

echo "== 6. wait for RUNNING =="
for _ in $(seq 1 60); do
  STATUS="$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" --query Service.Status --output text)"
  [ "$STATUS" = "RUNNING" ] && break
  [ "$STATUS" = "CREATE_FAILED" ] && { echo "   CREATE_FAILED; check App Runner logs"; exit 1; }
  sleep 10
done
URL="$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" --query Service.ServiceUrl --output text)"
echo
echo "== DONE =="
echo "  status:  $STATUS"
echo "  health:  https://$URL/health"
echo "  webhook: https://$URL/portkey/coding    <- point the Portkey guardrail here"
