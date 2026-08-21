#!/usr/bin/env bash
# Build and push the workflow agent container image to the ECR repository
# created by templates/ecs_deployment.yaml.
#
# Usage:
#   AWS_REGION=us-east-1 \
#   AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) \
#   STACK_NAME=nova-act-fleet-ecs \
#   IMAGE_TAG=v1 \
#   ./scripts/build_and_push.sh
#
# Defaults:
#   AWS_REGION    = us-east-1
#   STACK_NAME    = nova-act-fleet-ecs
#   IMAGE_TAG     = v1
#
# NOTE: The ECR repository is provisioned with ImageTagMutability=IMMUTABLE.
# A tag can be pushed only once. On subsequent builds, bump IMAGE_TAG
# (v2, v3, ...) or set it to a git short SHA, e.g.:
#   IMAGE_TAG=$(git rev-parse --short HEAD) ./scripts/build_and_push.sh
#
# The script:
#   1. Looks up the ECR repository URI from the CloudFormation stack outputs.
#   2. Authenticates the local Docker daemon to ECR.
#   3. Builds the image from the repository root (uses the project Dockerfile).
#   4. Tags and pushes the image as <repo>:<tag>.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-nova-act-fleet-ecs}"
IMAGE_TAG="${IMAGE_TAG:-v1}"

if [[ -z "${AWS_ACCOUNT_ID:-}" ]]; then
  AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${AWS_REGION}")"
fi

echo "Account:  ${AWS_ACCOUNT_ID}"
echo "Region:   ${AWS_REGION}"
echo "Stack:    ${STACK_NAME}"
echo "Image:    ${IMAGE_TAG}"

ECR_URI="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='ECRRepositoryUri'].OutputValue" \
  --output text)"

if [[ -z "${ECR_URI}" || "${ECR_URI}" == "None" ]]; then
  echo "ERROR: could not resolve ECRRepositoryUri output from stack ${STACK_NAME}." >&2
  echo "       Make sure templates/ecs_deployment.yaml is deployed first." >&2
  exit 1
fi

echo "ECR URI:  ${ECR_URI}"

aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Build for linux/amd64 because Fargate runs amd64 unless you opt in to arm64.
# The plain `docker build` form works under both Docker Desktop and Podman's
# docker shim (which is what macOS users on Apple Silicon often have).
# On arm64 hosts, Podman uses QEMU to emulate amd64; the first build takes
# noticeably longer than a native amd64 build.
docker build \
  --platform linux/amd64 \
  --tag "${ECR_URI}:${IMAGE_TAG}" \
  --file Dockerfile \
  .

docker push "${ECR_URI}:${IMAGE_TAG}"

echo
echo "Pushed: ${ECR_URI}:${IMAGE_TAG}"
