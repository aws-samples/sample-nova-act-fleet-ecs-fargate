# Operational Guide

Day-to-day operations guide for the Nova Act UI Automation framework. Covers monitoring, alerting, troubleshooting, regional and language constraints, compliance posture, and performance targets.

---

## Monitoring Setup

### CloudWatch Metrics

The Fleet Orchestrator publishes three custom metrics to the `NovaActFleet` CloudWatch namespace after every batch execution:

| Metric | Unit | Description |
|---|---|---|
| `TaskSuccessRate` | Percent | Ratio of succeeded tasks to total tasks in the batch |
| `AverageTaskDuration` | Seconds | Mean execution duration across all tasks in the batch |
| `HITLEscalationRate` | Percent | Ratio of tasks escalated to human operators to total tasks |

View metrics in the CloudWatch console:

```
CloudWatch → Metrics → Custom Namespaces → NovaActFleet
```

Or query via CLI:

```bash
aws cloudwatch get-metric-statistics \
  --namespace NovaActFleet \
  --metric-name TaskSuccessRate \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 900 \
  --statistics Average \
  --region us-east-1
```

### CloudWatch Log Groups

Structured JSON logs are emitted for every task execution. Each log entry includes `timestamp`, `task_id`, `event_type`, `status`, `message`, and `metadata`.

| Deployment Pattern | Log Group |
|---|---|
| ECS Fargate (workflow agent) | `/ecs/<env>-workflow-agent` |
| Fleet Orchestrator | `/<env>/agents` |

Default log retention is 90 days (configurable via `ObservabilityConfig.artifact_retention_days`).

Query logs with CloudWatch Logs Insights:

```sql
-- Find all failed tasks in the last hour
fields @timestamp, task_id, status, message
| filter event_type = "task_end" and status = "failed"
| sort @timestamp desc
| limit 50
```

```sql
-- Average task duration by task type
fields @timestamp, task_id, metadata.task_type, metadata.duration_seconds
| filter event_type = "task_end"
| stats avg(metadata.duration_seconds) as avg_duration by metadata.task_type
```

### S3 Artifacts

Execution artifacts (screenshots, extracted data, error reports) are stored in S3 under the configured prefix:

```
s3://<bucket>/<prefix>/
  ├── screenshots/
  │   └── <task_id>/<timestamp>.png
  ├── extracted_data/
  │   └── <task_id>/<timestamp>.json
  └── error_reports/
      └── <task_id>/<timestamp>.json
```

Configuration defaults (from `ObservabilityConfig`):
- **S3 prefix:** `nova-act-artifacts`
- **Retention:** 90 days (set via `artifact_retention_days`)

To list recent artifacts for a specific task:

```bash
aws s3 ls s3://<bucket>/nova-act-artifacts/ --recursive \
  | grep "<task_id>"
```

---

## Alerting Configuration

### Success Rate Alarm

The `ObservabilityManager.configure_alarms()` method creates a CloudWatch alarm that fires when the task success rate drops below 90% over a 15-minute evaluation window.

Alarm details:

| Setting | Value |
|---|---|
| Alarm name | `NovaActFleet-LowSuccessRate` |
| Namespace | `NovaActFleet` |
| Metric | `TaskSuccessRate` |
| Statistic | Average |
| Threshold | < 90% |
| Period | 15 minutes (900 seconds) |
| Evaluation periods | 1 |
| Missing data treatment | `notBreaching` |

To configure the alarm programmatically:

```python
from nova_act_fleet.components.observability_manager import ObservabilityManager
from nova_act_fleet.models.config import ObservabilityConfig

obs_config = ObservabilityConfig(
    cloudwatch_log_group="/nova-act-fleet/orchestrator",
    s3_bucket="my-artifact-bucket",
    alarm_success_rate_threshold=0.90,
    alarm_evaluation_period_minutes=15,
)
obs = ObservabilityManager(obs_config)
obs.configure_alarms()
```

To adjust the threshold or evaluation window, modify `alarm_success_rate_threshold` and `alarm_evaluation_period_minutes` in `ObservabilityConfig`.

### HITL Escalation Alerts

When a task exhausts all retries and is escalated to a human operator, the `HITLManager` publishes a notification to the configured SNS topic. The escalation payload includes:

- Original `TaskDefinition` (task ID, type, URL, prompt)
- Complete error history (all `ErrorDetail` objects from every retry attempt)
- S3 key of the last captured screenshot

To receive escalation alerts, subscribe to the SNS topic:

```bash
# Email subscription
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:<account-id>:nova-act-fleet-hitl \
  --protocol email \
  --notification-endpoint ops-team@example.com \
  --region us-east-1

# Lambda subscription (for automated triage by a customer-owned Lambda
# function — note that this project does not ship a Lambda triage
# function; see `docs/decision_matrix.md` for the v0.2 deployment surface)
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:<account-id>:nova-act-fleet-hitl \
  --protocol lambda \
  --notification-endpoint arn:aws:lambda:us-east-1:<account-id>:function:hitl-triage \
  --region us-east-1
```

### Recommended Additional Alarms

Consider adding these alarms for comprehensive coverage:

```bash
# HITL escalation rate exceeds 5%
aws cloudwatch put-metric-alarm \
  --alarm-name NovaActFleet-HighEscalationRate \
  --namespace NovaActFleet \
  --metric-name HITLEscalationRate \
  --statistic Average \
  --period 900 \
  --evaluation-periods 1 \
  --threshold 5 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --region us-east-1

# Average task duration exceeds 10 minutes
aws cloudwatch put-metric-alarm \
  --alarm-name NovaActFleet-HighTaskDuration \
  --namespace NovaActFleet \
  --metric-name AverageTaskDuration \
  --statistic Average \
  --period 900 \
  --evaluation-periods 1 \
  --threshold 600 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --region us-east-1
```

---

## Troubleshooting Procedures

### Common Failure Modes

| Failure Mode | Symptoms | Root Cause | Resolution |
|---|---|---|---|
| Domain validation rejection | Task fails immediately with `AgentSessionError` | Starting URL not in `allowed_domains` | Add the domain to `AgentConfig.allowed_domains` |
| Session timeout | Task returns status `TIMEOUT` after 30 min | Complex workflow exceeds session limit | Break the workflow into smaller tasks; reduce `session_timeout_seconds` to fail faster |
| Step limit exceeded | Task returns `FAILED` with step-limit error | Task requires > 100 sequential browser actions | Simplify the prompt or split into multiple tasks |
| Prompt too long | Task rejected with character limit error | Prompt exceeds 10,000 characters | Shorten the prompt; extract static data into `input_data` |
| Payload too large | Task status `REJECTED` in batch result | JSON-serialized task > 5 MB | Reduce `input_data` size; upload large data to S3 and pass a reference |
| ActError (element not found) | Retry loop, eventual `FAILED` status | Page structure changed or element not visible | Update the prompt to match current page layout; add wait times |
| CAPTCHA / security challenge | Non-retryable failure, HITL escalation | Target site detects automation | Use HITL resolution; consider allowlisting the automation IP |
| Browser crash in ECS | Task fails with infrastructure error | Insufficient shared memory for Chromium | Verify `SharedMemorySize: 2048` in the ECS task definition |

### Investigating a Failed Task

1. **Get the task ID** from the `BatchResult` or HITL escalation notification.

2. **Check CloudWatch Logs** for the task timeline:
   ```sql
   fields @timestamp, event_type, status, message
   | filter task_id = "<task-id>"
   | sort @timestamp asc
   ```

3. **Review error details** — look for the `task_end` event with `status = "failed"`:
   ```sql
   fields @timestamp, message, metadata
   | filter task_id = "<task-id>" and event_type = "error"
   | sort @timestamp asc
   ```

4. **Examine screenshots** stored in S3:
   ```bash
   aws s3 ls s3://<bucket>/nova-act-artifacts/ --recursive | grep "<task-id>"
   aws s3 cp s3://<bucket>/nova-act-artifacts/screenshots/<task-id>/ ./debug/ --recursive
   ```

5. **Check retry history** — the `TaskResult.error_details` list contains one `ErrorDetail` per attempt, each with `error_type`, `message`, `page_url`, and `screenshot_key`.

6. **If escalated to HITL**, check the SNS notification or DynamoDB table (if configured) for the escalation payload and any human resolution.

### Log Analysis Patterns

**Find tasks with the most retries:**
```sql
fields task_id, retry_count, status
| filter event_type = "task_end"
| sort retry_count desc
| limit 20
```

**Identify slow tasks:**
```sql
fields task_id, metadata.duration_seconds as duration, status
| filter event_type = "task_end"
| sort duration desc
| limit 20
```

**Track HITL escalation volume over time:**
```sql
fields @timestamp
| filter event_type = "escalation"
| stats count() as escalations by bin(1h)
```

**Find domain validation failures:**
```sql
fields @timestamp, task_id, message
| filter message like /domain/ and event_type = "error"
| sort @timestamp desc
```

---

## Regional and Language Constraints

### Regional Availability

Amazon Nova Act is available in **US East (N. Virginia) / `us-east-1`** at launch. All resources — VPCs, ECS clusters, ECR repositories, S3 buckets, CloudWatch log groups, and SNS topics — must be deployed in this region.

If your organization operates in other regions, consider:
- Deploying the orchestration layer in `us-east-1` and connecting to it from other regions via API Gateway or cross-region VPC peering.
- Storing final results in a regional S3 bucket after processing completes in `us-east-1`.

### Language Optimization

Natural language prompts are **optimized for English**. The Nova Act model performs best with clear, concise English instructions. When writing prompts:

- Use direct, imperative language: "Click the Submit button" rather than ambiguous phrasing.
- Avoid idioms, abbreviations, or domain-specific jargon that the model may not interpret correctly.
- For non-English web pages, write prompts in English that reference visible UI elements (button text, labels) in the target language. For example: "Click the button labeled 'Enviar'."

### Browser Requirement

All Workflow Agents use **Chromium-based browsers via Playwright** for browser automation. This is a hard requirement of the Nova Act SDK.

Implications by deployment pattern:

| Pattern | Browser Provisioning |
|---|---|
| ECS Fargate | The Docker image must include Chromium and Playwright (`python -m playwright install chromium`). The task definition allocates 4 vCPU and 8 GB RAM. Chromium uses Fargate's default 64 MB `/dev/shm` (Fargate doesn't support `LinuxParameters.SharedMemorySize`); for single-page Nova Act sessions this is enough. |

> If you can use AgentCore Browser, browser provisioning becomes managed and this whole row goes away — see [`why_not_agentcore.md`](../docs/why_not_agentcore.md) for whether that path is open to you.

---

## Compliance Documentation

### SOC 2 Coverage

Amazon Nova Act is covered under **AWS Bedrock SOC 2 compliance**. This means:

- The Nova Act Service inherits the SOC 2 Type II controls applied to the Amazon Bedrock platform.
- SOC 2 reports are available through [AWS Artifact](https://aws.amazon.com/artifact/) in the AWS Management Console.
- Coverage applies to the Nova Act Service itself. Your application code, VPC configuration, IAM policies, and data handling practices are your responsibility under the shared responsibility model.

### HIPAA and PCI-DSS

**HIPAA and PCI-DSS compliance are not supported at launch.** Specifically:

- Amazon Nova Act is **not** a HIPAA-eligible service at launch. Do not process Protected Health Information (PHI) through Workflow Agent browser sessions.
- Amazon Nova Act is **not** PCI-DSS certified at launch. Do not use Workflow Agents to process, store, or transmit cardholder data in production payment flows.

If your workloads require HIPAA or PCI-DSS compliance, wait for AWS to announce certification for these standards before deploying Nova Act in regulated environments.

### Staying Current

AWS compliance certifications evolve over time. To check the latest status:

- Visit the [AWS Compliance Programs](https://aws.amazon.com/compliance/programs/) page.
- Review the [AWS Services in Scope](https://aws.amazon.com/compliance/services-in-scope/) page for up-to-date service-level certification details.
- Download the latest SOC 2 report from [AWS Artifact](https://aws.amazon.com/artifact/).
- Contact your AWS account team or Solutions Architect for guidance on specific compliance requirements.

---

## Performance Targets

The framework is designed to meet the following operational targets:

| Target | Value | Metric | Alarm Threshold |
|---|---|---|---|
| Task success rate | ≥ 90% | `TaskSuccessRate` | < 90% triggers `NovaActFleet-LowSuccessRate` alarm |
| HITL escalation rate | < 5% | `HITLEscalationRate` | > 5% (recommended alarm) |
| Average task duration | < 10 minutes | `AverageTaskDuration` | > 600 seconds (recommended alarm) |
| Manual processing time reduction | ≥ 50% | Measured externally | — |
| Cost savings vs. traditional RPA | ≥ 80% | Measured externally | — |
| Security incidents from agent sessions | 0 | Incident tracking | — |

### Tuning for Performance

- **Concurrency:** Increase `FleetConfig.concurrency_limit` to process more tasks in parallel. Monitor CloudWatch metrics to ensure the Nova Act Service is not throttled.
- **Retries:** Set `TaskDefinition.max_retries` based on task criticality. Higher retry counts improve success rate but increase duration and cost.
- **Step wait times:** Use `TaskDefinition.step_wait_seconds` and `CheckoutFlowDefinition.step_wait_seconds` to accommodate slow-loading pages without unnecessary delays.
- **Session timeout:** Keep `AgentConfig.session_timeout_seconds` as low as practical for your workflows. The maximum is 1800 seconds (30 minutes).
- **Prompt quality:** Clear, specific prompts produce higher success rates. Avoid vague instructions. Test prompts iteratively and review screenshots from failed runs.

### Capacity Planning

- Each Workflow Agent occupies one thread in the `ThreadPoolExecutor`. Plan concurrency limits based on available compute (ECS service desired count and task quotas).
- Each browser session consumes approximately 4 vCPU and 8 GB RAM (ECS pattern). Size your Fargate task definitions accordingly.
- S3 artifact storage grows with task volume. Monitor bucket size and adjust `artifact_retention_days` to control costs.
- CloudWatch Logs costs scale with log volume. Use log retention policies and consider sampling verbose metadata in high-throughput scenarios.
