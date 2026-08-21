# Deployment Guide

Step-by-step instructions for deploying Nova Act Fleet on AWS. The project targets **Amazon ECS Fargate** for production browser-automation workloads. AWS Lambda was evaluated as an alternative compute target and intentionally cut from v0.2; see [`decision_matrix.md`](decision_matrix.md) § "Why no Lambda template?" for the rationale.

> AgentCore Browser is intentionally out of scope for this project. If AgentCore Browser fits your environment, use it instead. See [`why_not_agentcore.md`](why_not_agentcore.md) and the official [Nova Act + AgentCore Browser quickstart](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-quickstart-nova-act.html).

## Prerequisites

Before deploying, ensure the following are in place:

1. **AWS Account** with permissions to create VPCs, ECS clusters, IAM roles, S3 buckets, Secrets Manager secrets, ECR repositories, and CloudWatch resources.
2. **AWS CLI v2** installed and configured (`aws configure`) with a profile that has the required permissions.
3. **Python 3.8+** installed locally (3.12 recommended).
4. **Nova Act API key** — obtain from the Amazon Nova Act console. Two equivalent ways to store it in Secrets Manager; pick one and use it consistently.

   **Option 1 — JSON-wrapped (recommended; supports rotating multiple keys later):**
   ```bash
   aws secretsmanager create-secret \
     --name nova-act-api-key \
     --secret-string '{"nova-act-api-key":"<your-api-key>"}' \
     --region us-east-1
   ```
   The shipped ECS template extracts the value at JSON key `nova-act-api-key` via `ValueFrom: <arn>:nova-act-api-key::`. No template change needed.

   **Option 2 — Plain string:**
   ```bash
   aws secretsmanager create-secret \
     --name nova-act-api-key \
     --secret-string "<your-api-key>" \
     --region us-east-1
   ```
   With a plain string, change the `Secrets` block in `templates/ecs_deployment.yaml` to:
   ```yaml
   Secrets:
     - Name: NOVA_ACT_API_KEY
       ValueFrom: !Ref NovaActApiKeySecretArn
   ```

   Note the ARN returned — you will pass it as the `NovaActApiKeySecretArn` parameter.
5. **S3 bucket** for execution artifacts (screenshots, extracted data, error reports):
   ```bash
   aws s3 mb s3://<your-artifact-bucket-name> --region us-east-1
   ```
6. **Docker** (or Podman with a `docker` shim) installed and running locally to build the container image.
7. **Project dependencies** installed:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

> **Region note:** Amazon Nova Act is available in US East (N. Virginia) / `us-east-1` at launch. Deploy all resources in this region.

---

## Deployment: Amazon ECS Fargate

Best for production Nova Act browser-automation workloads. Fargate tasks get 4 vCPU and 8 GB RAM — enough for Chromium-based browser automation.

Template: `templates/ecs_deployment.yaml`. Helper script: `scripts/build_and_push.sh`.

### 1. Deploy the CloudFormation stack first

The stack creates the VPC, NAT gateway, ECS cluster, ECR repository, IAM roles, log group, task definition, and a service with `DesiredTaskCount=0` (so the service exists but starts no tasks until you have an image to pull).

```bash
aws cloudformation deploy \
  --template-file templates/ecs_deployment.yaml \
  --stack-name nova-act-fleet-ecs \
  --parameter-overrides \
    EnvironmentName=nova-act-fleet \
    S3ArtifactBucketName=<your-artifact-bucket-name> \
    NovaActApiKeySecretArn=<secret-arn> \
    ContainerImageTag=v1 \
    DesiredTaskCount=0 \
    HITLNotificationEmail=<optional-email@example.com> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

If you pass `HITLNotificationEmail`, AWS sends a confirmation email; the subscription is inactive until you click the confirmation link. Leave the parameter empty (or omit it) if you plan to subscribe endpoints out-of-band — see step 3 below.

**Template parameters reference:**

| Parameter | Default | Description |
|---|---|---|
| `EnvironmentName` | `nova-act` | Prefix for all resource names |
| `VpcCidr` | `10.0.0.0/16` | CIDR block for the VPC |
| `PublicSubnetCidr` | `10.0.1.0/24` | CIDR for the public subnet (NAT gateway) |
| `PrivateSubnetCidr` | `10.0.2.0/24` | CIDR for the private subnet (Fargate tasks) |
| `S3ArtifactBucketName` | — | Pre-existing S3 bucket for execution artifacts |
| `NovaActApiKeySecretArn` | — | Pre-existing Secrets Manager ARN holding the API key as a plain string |
| `ContainerImageTag` | `v1` | Docker image tag the task definition pulls. The ECR repository is immutable, so bump this on each deploy (`v2`, `v3`, or a git short SHA) |
| `DesiredTaskCount` | `0` | Running task count (0 = on-demand via `RunTask`) |
| `HITLNotificationEmail` | `''` | Optional email subscribed to the HITL SNS topic. Empty = no subscription created |
| `AlarmSuccessRateThreshold` | `90` | Percent threshold for the `TaskSuccessRate` alarm |
| `AlarmEvaluationPeriodSeconds` | `900` | Evaluation window for the alarm (must be a multiple of 60) |

### 2. Subscribe an endpoint to the HITL SNS topic

The stack creates an SNS topic (`<EnvironmentName>-hitl-escalations`) that receives:
- HITL escalations published by `HITLManager.escalate()` when a task exhausts retries.
- Notifications from the `TaskSuccessRateAlarm` CloudWatch alarm when fleet-wide success rate drops below the configured threshold.

The topic is encrypted at rest with the AWS-managed KMS key for SNS (`alias/aws/sns`). To use a customer-managed CMK, replace `KmsMasterKeyId` in the template with the CMK ARN and grant the ECS task role `kms:GenerateDataKey*` and `kms:Decrypt` on the CMK.

If you set `HITLNotificationEmail` in step 1, an email subscription is created automatically — confirm it via the link AWS emailed you and skip the rest of this step.

To subscribe additional endpoints (SQS, Lambda, Chatbot, extra emails), resolve the topic ARN and use `aws sns subscribe`:

```bash
HITL_TOPIC=$(aws cloudformation describe-stacks --stack-name nova-act-fleet-ecs \
  --query "Stacks[0].Outputs[?OutputKey=='HITLTopicArn'].OutputValue" \
  --output text --region us-east-1)

# Example: subscribe an SQS queue
aws sns subscribe \
  --topic-arn "$HITL_TOPIC" \
  --protocol sqs \
  --notification-endpoint arn:aws:sqs:us-east-1:<account-id>:<queue-name> \
  --region us-east-1
```

Without at least one subscription, HITL escalations and alarm notifications are published but no human or downstream system is notified.

### 3. Build and push the container image

The repository ships a `Dockerfile` and a `docker/entrypoint.py` that wraps `WorkflowAgent.execute_task`. Use the helper script to build for `linux/amd64` (Fargate's default architecture) and push to the ECR repository the stack created.

```bash
AWS_REGION=us-east-1 \
STACK_NAME=nova-act-fleet-ecs \
IMAGE_TAG=v1 \
./scripts/build_and_push.sh
```

The script reads the ECR URI from the stack's `ECRRepositoryUri` output, runs `aws ecr get-login-password` for Docker, builds with `docker buildx build --platform linux/amd64`, and pushes. The first build pulls the Python and Playwright base layers and takes 5–10 minutes; subsequent builds are seconds because the package install layer caches.

The ECR repository is configured with `ImageTagMutability=IMMUTABLE`, so a tag can only be pushed once. Bump `IMAGE_TAG` on each rebuild (`v2`, `v3`, or use a git short SHA: `IMAGE_TAG=$(git rev-parse --short HEAD)`) and update the `ContainerImageTag` stack parameter to match.

### 4. Run an on-demand task

Resolve the resources you'll need from stack outputs:

```bash
CLUSTER=$(aws cloudformation describe-stacks --stack-name nova-act-fleet-ecs \
  --query "Stacks[0].Outputs[?OutputKey=='ECSClusterName'].OutputValue" \
  --output text --region us-east-1)
TASK_DEF=$(aws cloudformation describe-stacks --stack-name nova-act-fleet-ecs \
  --query "Stacks[0].Outputs[?OutputKey=='TaskDefinitionArn'].OutputValue" \
  --output text --region us-east-1)
SUBNET=$(aws cloudformation describe-stacks --stack-name nova-act-fleet-ecs \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetId'].OutputValue" \
  --output text --region us-east-1)
SG=$(aws cloudformation describe-stacks --stack-name nova-act-fleet-ecs \
  --query "Stacks[0].Outputs[?OutputKey=='ECSTaskSecurityGroupId'].OutputValue" \
  --output text --region us-east-1)
```

Construct a task payload and run the task with a `containerOverrides` block that injects `TASK_PAYLOAD`:

```bash
TASK_PAYLOAD='{
  "task_id":         "ecs-smoke-001",
  "task_type":       "custom",
  "starting_url":    "https://nova.amazon.com/act",
  "prompt":          "Read the page heading and return it as plain text.",
  "allowed_domains": ["nova.amazon.com", "amazon.com"]
}'

aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"workflow-agent\",\"environment\":[{\"name\":\"TASK_PAYLOAD\",\"value\":$(echo "$TASK_PAYLOAD" | jq -Rs .)}]}]}" \
  --region us-east-1
```

`jq -Rs .` is just there to JSON-escape the payload safely. Take the `tasks[0].taskArn` from the response — you'll need it to follow the run.

### 5. Validate the deployment

1. Check stack status:
   ```bash
   aws cloudformation describe-stacks \
     --stack-name nova-act-fleet-ecs \
     --query "Stacks[0].StackStatus" \
     --region us-east-1
   ```
   Expected: `CREATE_COMPLETE`.

2. Tail the container logs while the task runs:
   ```bash
   aws logs tail /ecs/nova-act-fleet-workflow-agent --follow --region us-east-1
   ```
   You should see Nova Act SDK output (`start session`, `think(...)`, `agentClick(...)`, `return(...)`), followed by a one-line JSON `TaskResult` dump.

3. Confirm the task transitioned to `STOPPED` with `essentialContainerExited` and `0` exit code:
   ```bash
   aws ecs describe-tasks --cluster "$CLUSTER" --tasks <taskArn> --region us-east-1 \
     --query "tasks[0].{status: lastStatus, stopped: stoppedReason, exit: containers[0].exitCode}"
   ```

4. Check that screenshots landed in S3:
   ```bash
   aws s3 ls "s3://<your-artifact-bucket-name>/nova-act-artifacts/screenshots/" --region us-east-1
   ```

### 6. What gets created

The template provisions:
- VPC with one public and one private subnet, internet gateway, NAT gateway, EIP.
- ECS Fargate cluster with Container Insights enabled.
- Task definition: 4 vCPU, 8 GB RAM. Chromium runs with the default 64 MB `/dev/shm` Fargate provides; `LinuxParameters.SharedMemorySize` is not supported on Fargate.
- ECR repository with image scanning, AES-256 encryption, mutable tags, "keep last 10 images" lifecycle policy.
- Security group allowing outbound HTTPS only.
- IAM execution role (pull images, read the configured secret) and task role (CloudWatch Logs, S3 on the configured bucket, Secrets Manager on the configured secret, scoped CloudWatch Metrics on the `NovaActFleet` namespace, `sns:Publish` on the HITL topic).
- CloudWatch log group with 90-day retention.
- SNS topic (`<EnvironmentName>-hitl-escalations`) for HITL escalations and alarm notifications, with an optional email subscription created when `HITLNotificationEmail` is set.
- CloudWatch alarm (`<EnvironmentName>-TaskSuccessRate-Low`) on the `TaskSuccessRate` metric in the `NovaActFleet` namespace, wired to publish to the HITL topic on both breach and recovery.
- ECS service (set `DesiredTaskCount=0` for on-demand execution via `RunTask`).

---

## Common Troubleshooting

### Stack creation fails with IAM errors

Ensure you pass `--capabilities CAPABILITY_NAMED_IAM` when deploying. The templates create named IAM roles.

### Stack creation fails with `Fargate compatible task definitions do not support sharedMemorySize`

If you have an older copy of `templates/ecs_deployment.yaml` that includes `LinuxParameters.SharedMemorySize`, Fargate will reject the task definition. The shipped template removes that setting; Chromium runs against Fargate's default 64 MB `/dev/shm` and a single Nova Act page session fits comfortably. If you have a workload that needs more shared memory, override Chromium's launch arguments via a custom Playwright instance passed to `NovaAct(playwright_instance=...)` rather than re-introducing `SharedMemorySize`.

### Re-deploy fails with `AWS::EarlyValidation::ResourceExistenceCheck` after a previous rollback

The ECS template marks the ECR repository and the CloudWatch log group as `DeletionPolicy: Retain` to preserve images and logs across stack updates. After a `ROLLBACK_COMPLETE` failure, those resources survive and the next `aws cloudformation deploy` fails the early-validation hook because they already exist. Either delete them manually before re-deploying:

```bash
aws ecr delete-repository --repository-name nova-act-fleet-workflow-agent --force --region us-east-1
aws logs delete-log-group --log-group-name /ecs/nova-act-fleet-workflow-agent --region us-east-1
```

…or import them into the new stack with a CFN resource import operation.

### Nova Act fails at session start with `Failed to install Playwright browser binaries`

Even when the Dockerfile pre-installs Chromium at `PLAYWRIGHT_BROWSERS_PATH`, the Nova Act SDK runs an install check at session start and tries to re-download from the Playwright CDN. The security group blocks this by design (egress is HTTPS to `*` but the CDN check is timing-out / failing some other way). Set `NOVA_ACT_SKIP_PLAYWRIGHT_INSTALL=1` in the container env so the SDK trusts the pre-installed binaries. The shipped Dockerfile does this; if you have a custom image, add the same env var.

### Nova Act fails at startup with the API key authentication banner

Either the `NOVA_ACT_API_KEY` env var is empty, or the value contains the JSON wrapping (`{"nova-act-api-key":"..."}`) instead of the raw key. Two valid formats are supported in the deployment guide; pick one and use it consistently. Verify the running task has the right value with:

```bash
aws ecs describe-tasks --cluster <cluster> --tasks <task-arn> --region us-east-1 \
  --query "tasks[0].containers[0].name" --output text
```

…and check the corresponding container's environment in the task definition.

### `aws ecs run-task --overrides` rejects with `Container Overrides length must be at most 8192`

This is a hard ECS limit. For task payloads larger than ~8 KB, route them through SQS, S3, or DynamoDB and have the entrypoint dereference the location instead of inlining the payload as `TASK_PAYLOAD`. The shipped `docker/entrypoint.py` reads from `TASK_PAYLOAD` env var or stdin.

### ECS task starts but exits 0 with `status: failed` in the TaskResult

The container ran your task, the task hit an `ActError` or `ActAgentFailed` from Nova Act, and the framework recorded a `TaskResult(status=failed)`. Exit code 0 is the entrypoint's contract: "work was attempted, see the JSON output." Inspect the JSON in CloudWatch Logs for the failure reason.

### "Access Denied" when accessing S3 or Secrets Manager

- Confirm the `S3ArtifactBucketName` parameter matches the actual bucket name.
- Confirm the `NovaActApiKeySecretArn` parameter matches the actual secret ARN.
- Verify the IAM role policies in the CloudFormation template grant access to the correct resource ARNs.

### Cross-architecture builds on Apple Silicon are slow

The Dockerfile targets `linux/amd64` because Fargate's default is amd64. On Apple Silicon hosts running Podman or Docker Desktop, this means cross-arch QEMU emulation. The first clean build takes 7–10 minutes; subsequent rebuilds with cached Chromium and FFmpeg layers take seconds. If you're iterating on the entrypoint only, the build cache makes the rebuild ~30 seconds. If you change anything earlier in the Dockerfile (env vars, system packages), expect another full clean build.

### CloudWatch logs not appearing

- Verify the log group exists (the templates create it automatically).
- Check that the IAM role has `logs:CreateLogStream` and `logs:PutLogEvents` permissions on the correct log group ARN.

---

## Cleanup

To remove deployed resources:

```bash
# The ECR repository and CloudWatch log group are Retain by design;
# delete them by hand if you want them gone.
aws ecr batch-delete-image \
  --repository-name nova-act-fleet-workflow-agent \
  --image-ids imageTag=v1 \
  --region us-east-1
aws cloudformation delete-stack --stack-name nova-act-fleet-ecs --region us-east-1
```

For AgentCore Browser, this project doesn't ship infrastructure — see the [Nova Act + AgentCore Browser quickstart](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-quickstart-nova-act.html) and [`why_not_agentcore.md`](why_not_agentcore.md) for context on when that path is the right one.
