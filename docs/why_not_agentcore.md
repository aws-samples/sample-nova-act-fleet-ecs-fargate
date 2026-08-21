# When (not) to use this project instead of AgentCore Browser

Amazon Bedrock AgentCore Browser is the recommended way to run Nova Act in production for most teams. It provides managed Chromium sessions, session isolation, live viewing and replay, CloudTrail logging, and identity primitives out of the box. If those features fit your environment, **use AgentCore Browser** — start with the [Nova Act + AgentCore Browser quickstart](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-quickstart-nova-act.html) and the [`aws-samples/sample-browser-order-automation-agentcore`](https://github.com/aws-samples/sample-browser-order-automation-agentcore) sample.

This project exists for the cases where AgentCore Browser is not an option.

## Reasons to choose this project over AgentCore Browser

### 1. Region or account constraints

Nova Act and AgentCore both went GA recently and are still expanding regional coverage. If your account is in a region without AgentCore, or your organization has a hold on enabling Bedrock in production accounts, you cannot use AgentCore Browser today. Lambda and ECS Fargate are available in every commercial region.

### 2. Custom networking and data-residency requirements

AgentCore Browser runs in an AWS-managed environment. Some workloads cannot accept that:

- Egress must traverse a customer-owned NAT gateway, an on-premises firewall via Direct Connect, or a Transit Gateway with explicit allow-listed routes.
- Outbound traffic must be inspected by a third-party network appliance.
- Browser sessions must originate from a static, customer-controlled IP range allow-listed by upstream targets.
- All compute and data must remain inside a specific VPC and CIDR block, not a service-managed boundary.

This project places Nova Act sessions in your VPC and gives you the full networking surface — security groups, NACLs, NAT, VPC endpoints, Flow Logs.

### 3. Existing standardization on ECS / Lambda / CloudFormation

If your platform team has standardized on ECS Fargate or Lambda for compute, CloudFormation (or CDK / Terraform on top of it) for IaC, and CloudWatch + S3 + SNS for the operational stack, introducing AgentCore Runtime as an additional managed-service dependency may be a non-starter. This project stays inside the boxes those teams already operate.

### 4. Specific compliance audit posture

For some audits the easiest answer is "every IAM permission, every network route, and every log destination is in our account, defined in IaC, and reviewed by us." AgentCore's managed boundary is a different audit story — defensible, but different. If your auditors prefer "everything is in the customer account," this project is the simpler narrative.

### 5. Cost predictability for steady-state workloads

AgentCore is priced for managed-runtime convenience. ECS Fargate at sustained utilization, with an autoscaling floor, can be cheaper for steady high-volume fleets — especially when paired with Compute Savings Plans. Lambda is cheaper still for bursty, low-volume work. The decision matrix in [`decision_matrix.md`](decision_matrix.md) walks through the math.

### 6. Direct SDK control

A small but real reason: Nova Act SDK features ship in the SDK first and reach AgentCore Browser slightly later. If you need a brand-new SDK feature the day it lands, calling the SDK directly inside your own Lambda or ECS task is the shortest path.

## Reasons to choose AgentCore Browser instead

If any of the following are true, stop reading this repo and use AgentCore Browser:

- You have no strong VPC or networking requirements and don't want to operate one.
- You want managed session isolation and replay without building it yourself.
- Your workload is exploratory and fits naturally into the AgentCore Runtime programming model.
- Your team does not already operate ECS or Lambda at scale.
- You want the shortest possible path from "have an API key" to "agent runs in production."

For these teams, every minute spent on the security, networking, and observability scaffolding in this project is a minute they could have spent on the actual workflow. AgentCore Browser is the right call.

## What you give up by not using AgentCore Browser

This project's job is to replicate or accept the loss of each AgentCore Browser feature explicitly. The trade-offs are not free:

| AgentCore Browser feature | Trade-off in this project |
|---|---|
| Managed Chromium lifecycle | You operate the container image (build, push, patch). |
| Session isolation | You design the per-task ephemerality (we use a fresh `NovaAct` context per task; no shared state). |
| Live viewing and replay | You ship per-step screenshots to S3 and rely on the SDK's local HTML session log. No real-time browser inspection. |
| CloudTrail logging of agent actions | CloudTrail covers AWS API calls; the agent's natural-language reasoning is captured in CloudWatch Logs only. |
| AgentCore Identity (delegated credentials) | You wire credentials via Secrets Manager / SSM and rotate them yourself. |
| Domain restrictions enforced by the runtime | Enforced by `SecurityManager.validate_domain` in application code, *before* the session opens. The rest is on you. |

If those trade-offs read as "fine, we already do that for everything else we run," you're in the audience this project is for. If they read as "why would we redo all that ourselves," AgentCore Browser is the right call.

## Decision in one sentence

Use AgentCore Browser if you can; use this project when you can't, and bring the security, networking, and observability discipline that AgentCore would otherwise have given you for free.
