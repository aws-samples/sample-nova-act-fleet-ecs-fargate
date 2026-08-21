# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, please notify AWS/Amazon Security via the
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) or by email to
aws-security@amazon.com. Please do **not** create a public GitHub issue for security matters.

When submitting a report, please include:

- A clear description of the issue and its impact.
- Steps to reproduce, or a minimal proof of concept.
- The affected commit, release tag, or version.
- Your assessment of scope (data exposure, privilege escalation, denial of service, and so on).

We investigate all reports and coordinate remediation with reporters.

## Supported Versions

Only the latest minor release of `nova-act-fleet` receives security updates.

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | Yes                |
| < 0.2   | No                 |

## Shared Responsibility

This project is a reference pattern. AWS operates the security *of* the underlying services under the
[AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/). Adopters are
responsible for the security *in* their deployment, including:

- Rotating the Amazon Nova Act API key stored in AWS Secrets Manager on a schedule consistent with their compliance
  regime.
- Reviewing and restricting `AgentConfig.allowed_domains` before running workloads. Domain validation is exact-hostname
  match; wildcards are not supported.
- Enabling AWS Key Management Service (AWS KMS) encryption on the resources this project does not encrypt by default
  (see **Accepted Security Debt** below).
- Applying tighter, workload-specific IAM conditions on top of the shipped least-privilege roles.
- Monitoring the `TaskSuccessRate-Low` Amazon CloudWatch alarm, the CloudWatch log group, and the human-in-the-loop
  (HITL) Amazon SNS topic for anomalies.
- Running dependency and container-image scanning in their own CI before deploying to any regulated environment.
- Running AWS IAM Access Analyzer against the deployed `ECSTaskExecutionRole` and `ECSTaskRole` before promoting to
  production. External Access findings surface unintended cross-account or public exposure at deploy time; the Unused
  Access analyzer surfaces permissions that no live workload has exercised, typically after a 30–90 day operating
  window. Access Analyzer requires deployed roles as input, so it cannot be validated as part of the static review of
  this reference pattern — it is an explicit adopter responsibility. The pre-deployment checklist in
  [`docs/security_guide.md`](docs/security_guide.md) lists this as a required step.

## AWS Services Provisioned

The shipped AWS CloudFormation template (`templates/ecs_deployment.yaml`) provisions resources in the US East
(N. Virginia) `us-east-1` Region. Review the security posture of each before deploying:

- Amazon Virtual Private Cloud (single-AZ), internet gateway, NAT gateway with Elastic IP, public and private subnets,
  and route tables.
- Amazon Elastic Container Service cluster with Container Insights, an ECS service, and an AWS Fargate task definition
  (4 vCPU / 8 GB).
- Amazon Elastic Container Registry repository (image scanning on push enabled).
- IAM execution role and task role, each scoped to the named log group, secret, Amazon S3 bucket, the `NovaActFleet`
  CloudWatch metric namespace, and the HITL SNS topic.
- Security group with egress restricted to HTTPS on port 443 only.
- Amazon CloudWatch log group with 90-day retention, custom metrics under the `NovaActFleet` namespace, and a
  `TaskSuccessRate-Low` alarm wired to the HITL SNS topic.
- Amazon SNS topic for HITL escalations, with an optional email subscription.

The template does not create the Amazon S3 artifact bucket or the AWS Secrets Manager secret; the operator provisions
both and controls their encryption, bucket policy, and rotation configuration.

## Accepted Security Debt

The items below are known, documented trade-offs in the shipped template. Adopters should evaluate each for their
environment and layer additional controls where required.

- **Amazon ECR image layers are encrypted with SSE-S3 (AES256), not a customer-managed KMS key.** Layers are still
  encrypted at rest, but access to them cannot be revoked without deleting the repository, and image-pull events are
  not surfaced as `kms:Decrypt` calls in AWS CloudTrail. Adopters that need access auditing or independent revocation
  should switch the repository to a customer-managed CMK with rotation enabled. Replace the existing
  `EncryptionConfiguration` block on `ECRRepository` with:

  ```yaml
        EncryptionConfiguration:
          EncryptionType: KMS
          KmsKey: !GetAtt ECRKmsKey.Arn
  ```

  and add the following resources alongside it:

  ```yaml
  ECRKmsKey:
    Type: AWS::KMS::Key
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      Description: !Sub 'Customer-managed KMS key for ${EnvironmentName} ECR repository encryption'
      EnableKeyRotation: true
      KeyPolicy:
        Version: '2012-10-17'
        Statement:
          # Account root retains full administrative control. Day-to-day
          # administration is delegated via IAM identity policies attached
          # to admin principals in the account, not via key policy sprawl.
          - Sid: EnableRootAdmin
            Effect: Allow
            Principal:
              AWS: !Sub 'arn:${AWS::Partition}:iam::${AWS::AccountId}:root'
            Action: 'kms:*'
            Resource: '*'
          # ECS execution role pulls images at task start and must decrypt
          # layer data keys.
          - Sid: AllowECSExecutionRoleDecrypt
            Effect: Allow
            Principal:
              AWS: !GetAtt ECSTaskExecutionRole.Arn
            Action:
              - kms:Decrypt
              - kms:DescribeKey
            Resource: '*'

  ECRKmsKeyAlias:
    Type: AWS::KMS::Alias
    Properties:
      AliasName: !Sub 'alias/${EnvironmentName}-ecr'
      TargetKeyId: !Ref ECRKmsKey
  ```

  The image-pushing identity (a developer running `scripts/build_and_push.sh`, or a CI role) additionally needs
  `kms:GenerateDataKey` and `kms:Decrypt` on this CMK. Grant it via that principal's IAM identity policy, or by adding
  a corresponding statement to the key policy. A customer-managed CMK adds a small per-key monthly charge plus
  per-request KMS API costs; both are documented on the [AWS KMS pricing page](https://aws.amazon.com/kms/pricing/).
- **The `ECSTaskLogGroup` Amazon CloudWatch log group uses AWS-managed encryption, not a customer-managed CMK.**
  Structured task logs — including step-by-step Nova Act messages and any string data the agent extracts from a page —
  sit under the AWS-managed key for the 90-day retention window. That window bounds the exposure, but adopters who
  expect to log business-sensitive or personal data should switch the log group to a customer-managed CMK. The
  simplest path reuses the `ECRKmsKey` from the previous item. Two changes on top of that snippet:

  1. Add a service-principal statement to `ECRKmsKey.Properties.KeyPolicy.Statement` so CloudWatch Logs can use the
     key. The `Condition` scopes the grant to this log group's ARN so the key cannot be used to encrypt other log
     groups in the account:

     ```yaml
           - Sid: AllowCloudWatchLogsUseOfKey
             Effect: Allow
             Principal:
               Service: !Sub 'logs.${AWS::Region}.amazonaws.com'
             Action:
               - kms:Encrypt
               - kms:Decrypt
               - kms:ReEncrypt*
               - kms:GenerateDataKey*
               - kms:DescribeKey
             Resource: '*'
             Condition:
               ArnEquals:
                 'kms:EncryptionContext:aws:logs:arn': !Sub 'arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:${ECSTaskLogGroup}'
     ```

  2. Point `ECSTaskLogGroup` at the CMK:

     ```yaml
     ECSTaskLogGroup:
       Type: AWS::Logs::LogGroup
       DeletionPolicy: Retain
       UpdateReplacePolicy: Retain
       DependsOn: ECRKmsKey
       Properties:
         LogGroupName: !Sub '/ecs/${EnvironmentName}-workflow-agent'
         RetentionInDays: 90
         KmsKeyId: !GetAtt ECRKmsKey.Arn
     ```

  The explicit `DependsOn: ECRKmsKey` avoids a race during stack create — CloudWatch Logs validates its service-role
  access to the key at log-group creation time. Application code does not need any KMS permission for this change:
  `logs:PutLogEvents` continues to work as-is because CloudWatch Logs performs the KMS calls internally under the
  service-principal grant above.
- **`SecurityManager.get_iam_policy()` returns starter policies with `Resource: "*"` on every statement.** The
  helper's docstring warns callers, and the shipped Amazon CloudFormation template does not use these policies — it
  provisions its own fully scoped inline policies on `ECSTaskExecutionRole` and `ECSTaskRole`. That means neither the
  `LAMBDA` nor the `AGENTCORE` branch of this helper is wired into a deployed role in the v0.2 template, so there is
  no current blast radius. The debt sits in wait: a future release that lights up either pattern by attaching the
  helper's return value to a live role would ship a real over-broad grant. Adopters wiring up either pattern should
  narrow the resources before deployment.

  Recommended scoping per pattern:

  - **`DeploymentPattern.LAMBDA`.** Scope `lambda:InvokeFunction` to the specific function ARNs the caller invokes:
    `arn:aws:lambda:<region>:<account>:function:<function-name>` (or a wildcard suffix pattern the caller controls).
    The `ec2:CreateNetworkInterface`, `ec2:DescribeNetworkInterfaces`, and `ec2:DeleteNetworkInterface` statements
    must remain on `Resource: "*"` — those actions do not accept resource-level restrictions and AWS documents this
    as a hard limit for the VPC-Lambda ENI lifecycle. Narrow those by tag-based `Condition` blocks or an SCP if
    finer control is required, not by `Resource`.
  - **`DeploymentPattern.AGENTCORE`.** Scope `bedrock:InvokeModel` to the specific foundation-model ARNs the caller
    invokes, for example `arn:aws:bedrock:<region>::foundation-model/<model-id>`. If the caller uses a provisioned
    throughput or custom model, use the corresponding
    `arn:aws:bedrock:<region>:<account>:provisioned-model/<id>` or
    `arn:aws:bedrock:<region>:<account>:custom-model/<id>` form.
  - **`DeploymentPattern.ECS`.** The same wildcard pattern is in the ECS branch, but is already superseded by the
    scoped inline policies on `ECSTaskExecutionRole` and `ECSTaskRole` in `templates/ecs_deployment.yaml`. Adopters
    who deploy through the shipped template do not need to consume this branch of the helper. Adopters who deploy
    ECS imperatively should scope `ecs:RunTask/StopTask/DescribeTasks` to the specific cluster and task-definition
    ARNs, and the ECR actions to the workflow-agent repository ARN (see `ECRRepositoryPull` in the shipped template
    for the exact statement shape).

  A future change that scopes these branches natively — for example, by accepting an optional set of ARNs on
  `SecurityManager.get_iam_policy()` — would close the debt in code rather than in commentary.
- **Bandit `B110` (Try, Except, Pass) fires 11 times in
  `src/nova_act_fleet/components/workflow_agent.py`.** Every site guards a best-effort metadata capture —
  post-step screenshot upload, page-URL read, or CloudWatch log event emission — where a failure must not
  propagate into the task's result state. Two sites (post-execution screenshot capture and log-event emission)
  carry an inline rationale comment; the remaining nine follow an identical pattern in which the target
  variable is typed `Optional[str]` and initialized to `None` before the `try`, so the intent is clear from
  the surrounding shape rather than a per-site comment. None of the eleven sites swallows a real error path.
  The pattern is retained deliberately: propagating a screenshot-service or log-ingestion hiccup up into a
  running Nova Act task would surface as a task failure that misrepresents what actually went wrong. Adopters
  extending this pattern to new sites should either add an inline rationale comment, narrow the exception
  type, or attach a `# nosec B110` marker with justification to keep future scans easy to review.
- **The HITL Amazon SNS topic is encrypted with the AWS-managed KMS key for SNS (`alias/aws/sns`), not a
  customer-managed CMK.** HITL messages carry the task prompt, which may reference internal URLs or business context.
  Adopters that require a customer-managed CMK should replace `KmsMasterKeyId` in the template with their CMK ARN and
  grant `kms:GenerateDataKey*` and `kms:Decrypt` on the CMK to the ECS task role.
- **Single-AZ Amazon VPC.** The shipped template deploys to one Availability Zone (`us-east-1a`) to keep the reference
  pattern minimal. Adopters that require higher availability should extend the template to multi-AZ subnets, route
  tables, and NAT gateways.
- **Nova Act API key lifecycle in the task.** The Nova Act API key is loaded from AWS Secrets Manager and injected into
  the Fargate task as an environment variable at task start. The value is present in the task environment for the
  lifetime of the task. Rotate the secret on the schedule your compliance regime requires.
- **Task payload size ceiling.** `aws ecs run-task --overrides` caps container overrides at 8,192 bytes. Larger task
  payloads must be routed through Amazon SNS, Amazon SQS, Amazon S3, or Amazon DynamoDB and dereferenced from inside
  the container. The reference pattern does not implement this dereference path; adopters shipping large payloads
  should add it.
- **VPC Flow Logs are not enabled on the shipped Amazon VPC** (cdk-nag `AwsSolutions-VPC7`). The reference pattern
  runs a single private subnet whose security group allows only HTTPS egress, which limits the blast radius of
  suspicious traffic but leaves no network-level audit trail. Adopters extending the pattern to more complex traffic
  patterns, or subject to network-audit obligations, should enable flow logs. The minimum change is three resources
  (an `AWS::EC2::FlowLog`, a dedicated `AWS::Logs::LogGroup`, and an `AWS::IAM::Role` that the flow-log service
  assumes to write log events). Add the following to `templates/ecs_deployment.yaml`, alongside the existing VPC
  resources:

  ```yaml
  VPCFlowLogGroup:
    Type: AWS::Logs::LogGroup
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      LogGroupName: !Sub '/vpc/${EnvironmentName}-flow-logs'
      RetentionInDays: 90

  VPCFlowLogRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${EnvironmentName}-vpc-flow-log-role'
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: vpc-flow-logs.amazonaws.com
            Action: sts:AssumeRole
      Policies:
        - PolicyName: !Sub '${EnvironmentName}-vpc-flow-log-policy'
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action:
                  - logs:CreateLogStream
                  - logs:PutLogEvents
                  - logs:DescribeLogStreams
                Resource:
                  - !GetAtt VPCFlowLogGroup.Arn
                  - !Sub '${VPCFlowLogGroup.Arn}:*'

  VPCFlowLog:
    Type: AWS::EC2::FlowLog
    Properties:
      ResourceType: VPC
      ResourceId: !Ref VPC
      TrafficType: ALL
      LogDestinationType: cloud-watch-logs
      LogGroupName: !Ref VPCFlowLogGroup
      DeliverLogsPermissionArn: !GetAtt VPCFlowLogRole.Arn
  ```

  Flow logs incur ingestion and storage charges on the `VPCFlowLogGroup` log group. The 90-day retention above matches
  the ECS task log group; tune to match your organization's retention policy. To capture rejected traffic only, set
  `TrafficType: REJECT`.

## Compliance

This pattern is a reference implementation and is **not currently eligible for HIPAA** and is **not certified for
PCI DSS**. Amazon Nova Act inherits Amazon Bedrock SOC 2 coverage. See the
[AWS Compliance Programs](https://aws.amazon.com/compliance/programs/) page for the current list of programs and
services in scope.

## Dependencies

Runtime dependencies (see `pyproject.toml` and `Dockerfile`):

- Python 3.12 in the container image; Python 3.8+ for development.
- `nova-act`, `boto3`, `pydantic>=2.0`, `python-dotenv`.
- Playwright with Chromium (baked into the container image).

Adopters should run dependency scanning (`pip-audit`, `safety`, or an equivalent) and container-image scanning as part
of their own CI before promoting to production.

## Secret Scanning Baseline

The repository ships a `.secrets.baseline` at the root, generated by
[`detect-secrets`](https://github.com/Yelp/detect-secrets). The baseline records four known false positives (three
placeholder API-key strings in examples and tests, one substring match on the IAM action `secretsmanager:GetSecretValue`),
each marked `is_secret: false`. Adopters extending the code should re-audit with
`detect-secrets scan --baseline .secrets.baseline` before committing; new hits appear in the baseline diff and must be
reviewed. This is the same file [Automated Security Helper](https://github.com/awslabs/automated-security-helper) reads
by default, so re-running ASH does not re-flag the recorded false positives.

## Teardown

Deleting the CloudFormation stack does not remove the following retained resources. Manually delete each to complete
cleanup:

- Amazon ECR repository (`DeletionPolicy: Retain`).
- Amazon CloudWatch log group (`DeletionPolicy: Retain`).
- Any Amazon S3 objects under the `nova-act-artifacts/` prefix in the operator-provided bucket.
- The AWS Secrets Manager secret that stored the Nova Act API key.

Retained resources may continue to incur charges after stack deletion.
