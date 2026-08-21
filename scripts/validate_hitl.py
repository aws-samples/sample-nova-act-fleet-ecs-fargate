#!/usr/bin/env python3
"""Post-deployment HITL escalation validation.

Operator utility that verifies the HITL escalation path — from
``FleetOrchestrator`` through ``HITLManager`` to the SNS topic —
is wired up correctly after a stack deploy.

It submits a synthetic batch that is guaranteed to fail every retry,
so the orchestrator escalates each task to HITL and ``HITLManager``
publishes to the configured SNS topic. This exercises:

    FleetOrchestrator._escalate_to_hitl
      -> HITLManager.escalate
      -> boto3 SNS publish
      -> subscribers (email / SQS / Lambda / Chatbot)

Nova Act and Chromium are **not** invoked. The workflow agent is
replaced locally with a stub that always returns FAILED, so this
script uses your workstation's AWS credentials and does not touch
Fargate, ECR, or the Nova Act service.

Prerequisites
-------------
- AWS credentials with ``sns:Publish`` on the target topic.
- ``SNS_TOPIC_ARN`` env var pointing at the HITL topic. Grab it from
  the CFN stack outputs::

      export SNS_TOPIC_ARN=$(aws cloudformation describe-stacks \\
          --stack-name nova-act-fleet-ecs \\
          --query "Stacks[0].Outputs[?OutputKey=='HITLTopicArn'].OutputValue" \\
          --output text --region us-east-1)

- At least one **confirmed** subscriber on the topic (e.g. your email)
  so you can observe the escalation payload actually landing.

Run
---
    python scripts/validate_hitl.py

The script prints the resulting BatchResult summary. You should see
one SNS message per escalated task on your subscribers.
"""

import os
from datetime import datetime, timezone

from nova_act_fleet.agent import (
    AgentConfig,
    DeploymentPattern,
    FleetConfig,
    FleetOrchestrator,
    HITLConfig,
    HITLManager,
    TaskDefinition,
)
from nova_act_fleet.models.tasks import ErrorDetail, TaskResult, TaskStatus


class _AlwaysFailingAgent:
    """Local stand-in for ``WorkflowAgent`` used only by this validation script.

    Returns FAILED with ``retry_count == task.max_retries``, which is the
    condition ``FleetOrchestrator._escalate_to_hitl`` checks before
    forwarding to HITLManager.
    """

    def execute_task(self, task: TaskDefinition) -> TaskResult:
        now = datetime.now(timezone.utc)
        return TaskResult(
            task_id=task.task_id,
            status=TaskStatus.FAILED,
            started_at=now,
            completed_at=now,
            duration_seconds=0.0,
            retry_count=task.max_retries,
            page_url=task.starting_url,
            error_details=[
                ErrorDetail(
                    error_type="ForcedFailure",
                    message="Synthetic failure for HITL validation.",
                    page_url=task.starting_url,
                    timestamp=now,
                )
            ],
        )


def main() -> int:
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN", "").strip()
    if not sns_topic_arn:
        print(
            "ERROR: SNS_TOPIC_ARN is not set. Export it from your stack outputs:\n"
            "  export SNS_TOPIC_ARN=$(aws cloudformation describe-stacks \\\n"
            "      --stack-name nova-act-fleet-ecs \\\n"
            "      --query \"Stacks[0].Outputs[?OutputKey=='HITLTopicArn'].OutputValue\" \\\n"
            "      --output text --region us-east-1)"
        )
        return 1

    # api_key is unused by _AlwaysFailingAgent but AgentConfig still
    # requires a truthy value for validation.
    agent_config = AgentConfig(
        starting_url="https://example.com/",
        api_key="hitl-validation-nova-act-unused",
        allowed_domains=["example.com"],
        headless=True,
    )

    fleet_config = FleetConfig(
        concurrency_limit=2,
        agent_config=agent_config,
        deployment_pattern=DeploymentPattern.ECS,
        hitl_enabled=True,
    )

    orchestrator = FleetOrchestrator(config=fleet_config)
    orchestrator.set_workflow_agent(_AlwaysFailingAgent())
    orchestrator.set_hitl_manager(
        HITLManager(HITLConfig(sns_topic_arn=sns_topic_arn))
    )

    tasks = [
        TaskDefinition(
            task_id=f"hitl-validate-{i:03d}",
            task_type="custom",
            starting_url="https://example.com/",
            prompt=f"Synthetic HITL validation task {i}",
            max_retries=0,  # retry_count (0) >= max_retries (0) -> escalate
        )
        for i in range(1, 3)
    ]

    print(f"Publishing {len(tasks)} synthetic escalation(s) to:")
    print(f"  {sns_topic_arn}")
    print()

    batch = orchestrator.submit_batch(tasks)

    print(f"Batch ID:       {batch.batch_id}")
    print(f"Total tasks:    {batch.total_tasks}")
    print(f"Succeeded:      {batch.succeeded}")
    print(f"Failed:         {batch.failed}")
    print(f"Escalated:      {batch.escalated}")
    print(f"Rejected:       {batch.rejected}")
    print(f"Duration:       {batch.duration_seconds:.2f}s")
    print()

    if batch.escalated == 0:
        print(
            "ERROR: no tasks were escalated. The most likely causes are:\n"
            "  - hitl_enabled is False on FleetConfig\n"
            "  - retry_count < max_retries on the synthetic result\n"
            "  - HITLManager was not injected before submit_batch"
        )
        return 1

    print(
        f"OK. Check your SNS subscribers for {batch.escalated} escalation "
        "message(s). If you subscribed an email address, look for a "
        "message with subject 'HITL Escalation: hitl-validate-XXX'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
