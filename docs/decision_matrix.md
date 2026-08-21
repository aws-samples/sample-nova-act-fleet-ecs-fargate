# Deployment decision matrix

This document explains the compute target choice for the Nova Act Fleet project. The project ships **one** CloudFormation template, [`templates/ecs_deployment.yaml`](../templates/ecs_deployment.yaml), targeting Amazon ECS on AWS Fargate. AWS Lambda and Amazon Bedrock AgentCore Browser are evaluated below as alternatives that this project intentionally does not target in v0.2.

## Why Amazon ECS Fargate

ECS Fargate is the right primary target for production Nova Act workloads because:

- It supports session durations beyond Lambda's 15-minute hard ceiling. Most Nova Act prompts complete in 30–120 seconds, but multi-page checkout flows, QA suites, and prompts that wait on slow external sites can exceed 15 minutes. With Fargate, the bound is whatever you set in `AgentConfig.session_timeout_seconds` (max 1800 s in this framework).
- It allocates the CPU and RAM Chromium needs out of the box. The shipped task definition is 4 vCPU and 8 GB RAM. Lambda envelopes are smaller and Lambda's memory model is harder to reason about for browser automation.
- It runs in your own VPC under your own IAM, which is the entire point of this project (replicate AgentCore Browser's primitives in customer-controlled infrastructure).
- It scales horizontally via ECS service autoscaling or, for batch workloads, via `aws ecs run-task` invocations from a controller (Lambda, Step Functions, EventBridge).
- The container surface — Dockerfile, ECR, task definition — is what platform teams already operate. There is no managed-runtime dependency to introduce.

## Why no Lambda template?

A Lambda-based deployment was evaluated and intentionally cut from v0.2. The reasoning, briefly:

- **Container-image Lambda is required.** Nova Act drives Chromium via Playwright. The package, the Chromium binary, and the FFmpeg helper together blow through Lambda's 250 MB zip and 250 MB layer ceilings. The realistic path is a container-image Lambda (10 GB ceiling), which means a Dockerfile, an ECR repository, and a `handler.py` — most of the same surface as the ECS path with less leverage.
- **Cold-start cost is non-trivial for browser workloads.** Lambda VPC ENI attach plus container-image cold start plus Chromium boot can push first-invocation latency to 30–60 seconds. For tasks that are themselves only 30–120 seconds, the cold-start fraction is large enough to undermine Lambda's per-invocation pricing advantage.
- **The 15-minute hard ceiling is the wrong limit for Nova Act.** Multi-page checkouts and waits on slow upstream sites legitimately need more than 15 minutes. Lambda's ceiling means split-and-resume logic, which is more complexity than ECS Fargate.
- **The duplicated work is not justified.** A Lambda template would reproduce 80 percent of the ECS template (VPC, NAT, IAM, CloudWatch, S3, Secrets Manager) for the 20 percent of workloads where Lambda's pricing genuinely wins. Shipping one path that demonstrably works end-to-end is more valuable than two paths where one is half-finished.

The framework's data model still exposes `DeploymentPattern.LAMBDA` for forward compatibility. A contributor adding a Lambda path would need to write a container-image Lambda (Playwright Chromium exceeds the zip and layer size limits), publish a `handler.py` that mirrors `docker/entrypoint.py`, and provision the IAM, ECR, VPC, and log-group scaffolding the ECS template already covers. If you have a workload that genuinely fits Lambda's profile — strictly bursty, well under 15 minutes, no requirement to keep a browser session warm across calls — that's the outline. Otherwise, ECS Fargate at low desired count plus on-demand `RunTask` is the recommended path for low-volume burst workloads as well.

## Why not AgentCore Browser

If AgentCore Browser fits your environment, **use it instead** of this project. See [`why_not_agentcore.md`](why_not_agentcore.md). AgentCore Browser provides managed session isolation, live viewing, replay, CloudTrail of agent actions, identity primitives, and built-in scaling without you operating any of it. This project only makes sense when one or more of the constraints listed in `why_not_agentcore.md` apply: regulated workloads with custom networking and data-residency requirements, AWS accounts without Bedrock or AgentCore availability in your region, or organizations standardized on ECS / CloudFormation / CloudWatch that don't want to introduce a separate managed agent runtime into the stack.

## At a glance

| Dimension | Amazon ECS Fargate (this project) | AWS Lambda (deferred) | AgentCore Browser (out of scope) |
|---|---|---|---|
| Max session duration | Hours; bounded by `session_timeout_seconds` (max 30 min in framework) | 15 min hard ceiling | Hours (managed) |
| Cold-start latency | 30–60 s (image pull + ENI attach + container start) | 30–60 s for container-image Lambda | Managed; varies |
| Concurrency model | Long-running tasks, autoscaled service, or on-demand `RunTask` | Event-driven, pay-per-invocation | Managed |
| Networking | VPC private subnet + NAT (in this project) | VPC private subnet + NAT (would be the same) | AgentCore-managed |
| Container image management | Customer-built ECR image | Container-image Lambda required | None (managed) |
| Scaling ceiling | ECS service desired count + task quotas | Lambda concurrency quota (default 1k, raisable) | Managed; service quotas |
| Best for | Long-running flows, sustained throughput, GPU-free Chromium workloads, on-demand bursts via `RunTask` | Genuinely bursty, sub-15-minute, event-driven | Most teams that don't have specific reasons to build it themselves |
| Worst for | Tiny, very-bursty workloads with significant idle (NAT cost dominates) | Multi-page flows beyond 15 min; sustained high throughput | Custom networking, data-residency, region constraints |
| Out-of-the-box session isolation | Per-task container | Per-invocation container (cold start each time) | Native, by design |
| Out-of-the-box replay | None (use S3 screenshots + SDK local log) | None (same) | Native, with live view |
| Status in v0.2 | Shipped, deployable end-to-end | Deferred (see above) | Out of scope by design |

## Cost guidance

Real numbers depend on your task volume, prompt complexity, and the page weight of the sites you drive. Use these as rough order-of-magnitude estimates per 1,000 tasks at the time of writing, and verify with the [AWS Pricing Calculator](https://calculator.aws/) before committing.

### Reference timings from the v0.2 deploy

These are measured numbers from a real deploy of the ECS Fargate template into a sandbox account, driving `https://nova.amazon.com/act` with a mix of prompt shapes. They are illustrative — your numbers will move with prompt complexity, page weight, and target site responsiveness.

| Phase | Wall-clock |
|---|---|
| `aws cloudformation deploy` (cold) | ~5 min (NAT Gateway provisioning dominates) |
| `docker build` (clean, cross-arch QEMU on Apple Silicon) | ~7 min (Chromium + FFmpeg base layers) |
| `docker build` (incremental, only entrypoint changed) | ~30 s |
| `docker push` (~600 MB image) | ~30 s |
| Fargate task cold start (image pull + ENI attach + container start) | 30–45 s |
| Single Nova Act session, simple prompt (read heading) | 5–8 s of model work, ~15 s wall-clock |
| Single Nova Act session, multi-step (dismiss popup, navigate, extract) | 25 s of model work, ~30 s wall-clock |
| 3-task batch via `FleetOrchestrator`, `concurrency_limit=3` | 18 s wall-clock vs ~42 s sequential — `ThreadPoolExecutor` parallelism verified |

### Cost components per task

Each Nova Act task in this project incurs:

- **One Nova Act SDK call** — billed by Nova Act's per-call pricing. See the [Nova Act page](https://aws.amazon.com/nova/act/) for current pricing; this is the dominant cost component for low-AWS-overhead workloads.
- **One Chromium session** running for ~30–120 seconds (typical) up to 1800 seconds (timeout).
- **CloudWatch Logs `PutLogEvents`** — small, dominated by base log-ingest tier.
- **CloudWatch Metrics `PutMetricData`** — three metrics per batch.
- **S3 `PutObject`** for screenshots and artifacts — cents per 1k tasks.
- **NAT Gateway data transfer** — this is the AWS surprise cost on egress-heavy browser workloads. Each Nova Act session can pull 5–20 MB of page assets. NAT egress in `us-east-1` is roughly $0.045 per GB plus $0.045 per hour per NAT. For 1,000 tasks at 10 MB each, that's about $0.45 in data transfer plus the running NAT hourly cost.

> **Reference number from the v0.2 deploy:** the smoke-test deploy ran for ~3 hours (deploy → 5 successful Nova Act runs → teardown). NAT Gateway total cost was approximately $0.13 — about $0.10 for hourly charges and $0.03 for data transfer. The dominant infrastructure cost over a short session is hours-of-NAT, not bytes-of-NAT.

ECS Fargate-specific:

- **Per-task-hour cost** — at 4 vCPU / 8 GB, roughly $0.20–$0.25 per hour.
- **Idle-task cost** — if you keep tasks running between batches for warm-start latency, you pay for the idle time. The shipped template uses `DesiredTaskCount=0` (on-demand via `RunTask`) by default, so there is no idle cost until you opt in.
- For sustained high-volume workloads, pair the cluster with a Compute Savings Plan to reduce the per-task-hour rate.

AgentCore Browser-specific (for reference only; not used by this project):

- **Per-session cost** — see the [AgentCore pricing page](https://aws.amazon.com/bedrock/agentcore/pricing/). You pay for the managed runtime; you do not pay for VPC, NAT, or Fargate task-hours. For non-VPC-constrained workloads this often comes out cheaper than ECS Fargate at low to moderate volumes.

The honest takeaway: at small volumes, AgentCore Browser tends to be cheapest *and* easiest. At very high sustained volumes, ECS Fargate with Compute Savings Plans can win. Self-hosting on ECS only beats AgentCore on cost when you have very high utilization, and even then the operational surface is what tips the math — not the AWS bill.

## Service quotas to raise before going to production

| Service | Quota | Default | Why |
|---|---|---|---|
| Amazon ECS | Tasks per service | 5,000 | Mostly relevant only for very large fleets |
| Amazon ECR | Image pull throughput | Unspecified soft limit | High concurrency cold starts can throttle pulls; mitigate with image caching |
| Amazon CloudWatch Logs | `PutLogEvents` rate | 5 requests/second per log stream | This project uses one stream per process; sharded streams help if you batch heavily |
| Amazon Bedrock | TPS for the underlying model | Account-specific | Nova Act calls Bedrock under the hood; throttling shows up as `ActError` |
| Nova Act | Per-account session quota | See the [Nova Act docs](https://docs.aws.amazon.com/nova-act/) | Fleet concurrency above this gets throttled |

## Decision in two questions

1. **Can your account use AgentCore Browser today?**
   - Yes, no objections from networking, security, or compliance → **Use AgentCore Browser.** Stop here.
   - No, or there are real objections → **Use this project's ECS Fargate template.**

2. **Do you have a workload that strictly fits Lambda's profile and ECS Fargate's `RunTask`-on-demand mode is unsuitable?**
   - Yes → implement the Lambda path yourself using the outline in "Why no Lambda template?" above.
   - No → ECS Fargate with `DesiredTaskCount=0` and on-demand `RunTask` covers bursty workloads at low cost without the duplication.
