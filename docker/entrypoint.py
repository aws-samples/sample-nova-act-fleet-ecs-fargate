#!/usr/bin/env python3
"""Container entrypoint for the Nova Act Fleet workflow agent.

Reads a task payload from the ``TASK_PAYLOAD`` environment variable (or
stdin if empty), dispatches it as either a single task or a batch, and
prints the result as JSON to stdout.

Two payload shapes are supported:

  Single task::

      {
        "task_id":         "...",
        "task_type":       "custom" | "form" | ...,
        "starting_url":    "https://...",
        "prompt":          "...",
        "allowed_domains": ["..."],
        "blocked_domains": ["..."],     # optional
        "max_retries":     3,            # optional
        "input_data":      {...}         # optional
      }

  Multi-step checkout flow (one browser session, one screenshot per step)::

      {
        "task_id":           "...",
        "task_type":         "checkout_flow",
        "starting_url":      "https://...",
        "allowed_domains":   ["..."],
        "blocked_domains":   ["..."],     # optional
        "step_wait_seconds": 2.0,          # optional
        "steps": [
          {"step_name": "cart_review", "prompt": "..."},
          ...
        ]
      }

  Multi-step QA test (one browser session, one screenshot per step)::

      {
        "task_id":         "...",
        "task_type":       "qa_test",
        "test_name":       "...",          # optional, defaults to task_id
        "starting_url":    "https://...",
        "allowed_domains": ["..."],
        "steps": [
          {"action_prompt": "...", "expected_outcome": "..."},
          ...
        ]
      }

  Batch (FleetOrchestrator path, single-prompt tasks only)::

      {
        "tasks": [ {single-task-payload...}, ... ],
        "concurrency_limit": 3,                # optional, default 5
        "starting_url":      "https://...",    # optional shared override
        "allowed_domains":   ["..."]            # optional shared override
      }

The single-task path uses ``WorkflowAgent.execute_task`` and prints a
``TaskResult`` JSON. The ``checkout_flow`` and ``qa_test`` paths use
``WorkflowAgent.run_checkout_flow`` / ``run_qa_test`` and print a
``CheckoutResult`` / ``QATestReport`` JSON. The batch path uses
``FleetOrchestrator.submit_batch`` and prints a ``BatchResult`` JSON
containing all per-task results.

Every path uploads screenshots to ``nova-act-artifacts/screenshots/<task_id>/``
and the Nova Act SDK per-step session trace to
``nova-act-artifacts/traces/<task_id>/`` in ``S3_ARTIFACT_BUCKET``. Set
``CAPTURE_SCREENSHOTS=false`` to disable both.

The entry point refuses to start when ``AWS_REGION`` or ``AWS_DEFAULT_REGION``
names a Region other than ``us-east-1``: Amazon Nova Act is available only in
US East (N. Virginia), and the SDK always calls its us-east-1 endpoint.

Environment contract (set by the ECS task definition or `aws ecs run-task`
overrides):

  - NOVA_ACT_API_KEY   — Nova Act API key, injected from Secrets Manager.
  - S3_ARTIFACT_BUCKET — S3 bucket for screenshots and other artifacts.
  - ENVIRONMENT        — Logical environment name (default ``nova-act``).
  - TASK_PAYLOAD       — JSON body, single task or batch (see above).
  - SNS_TOPIC_ARN      — Optional. When set, HITL escalations for
                         retry-exhausted tasks are published to this
                         topic (only meaningful for batch payloads).
  - CAPTURE_SCREENSHOTS — Optional, default ``true``. ``false`` disables
                         screenshot and session-trace upload.
  - CLOUDWATCH_LOG_GROUP — Optional. Defaults to
                         ``/ecs/<ENVIRONMENT>-workflow-agent``, the log group
                         the shipped template creates.

Exit codes:
  0  — Work was attempted. Inspect the printed JSON for per-task status.
  1  — Configuration or input error before any task could run.
"""

import json
import os
import sys
from typing import Any, Dict, List

from pydantic import ValidationError

from nova_act_fleet.agent import (
    AgentConfig,
    AgentSessionError,
    CheckoutFlowDefinition,
    CheckoutStep,
    DeploymentPattern,
    FleetConfig,
    FleetOrchestrator,
    HITLConfig,
    HITLManager,
    ObservabilityConfig,
    QATestDefinition,
    QATestStep,
    TaskDefinition,
    WorkflowAgent,
)
from nova_act_fleet.components.observability_manager import ObservabilityManager

NOVA_ACT_REGION = "us-east-1"
MULTI_STEP_TASK_TYPES = ("checkout_flow", "qa_test")


def _read_payload() -> Dict[str, Any]:
    raw = os.environ.get("TASK_PAYLOAD", "").strip()
    if not raw:
        raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError(
            "No task payload found. Set TASK_PAYLOAD env var or pipe JSON to stdin."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"TASK_PAYLOAD is not valid JSON: {exc}") from exc


def _require(env: str) -> str:
    value = os.environ.get(env, "").strip()
    if not value:
        raise ValueError(f"Required environment variable {env!r} is not set.")
    return value


def _build_task(payload: Dict[str, Any]) -> TaskDefinition:
    return TaskDefinition(
        task_id=payload["task_id"],
        task_type=payload.get("task_type", "custom"),
        starting_url=payload["starting_url"],
        prompt=payload["prompt"],
        input_data=payload.get("input_data"),
        max_retries=payload.get("max_retries", 3),
    )


def _union_domains(tasks: List[Dict[str, Any]]) -> List[str]:
    """Return the union of every task's ``allowed_domains`` list."""
    seen: List[str] = []
    for task in tasks:
        for domain in task.get("allowed_domains", []):
            if domain not in seen:
                seen.append(domain)
    return seen


def _check_region() -> None:
    """Fail fast when the container is configured for another Region."""
    for env in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        region = os.environ.get(env, "").strip()
        if region and region != NOVA_ACT_REGION:
            raise ValueError(
                f"{env}={region!r}, but Amazon Nova Act is available only in "
                f"{NOVA_ACT_REGION} (US East, N. Virginia). Deploy the stack and run the container in "
                f"{NOVA_ACT_REGION}."
            )


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _make_observability(environment: str, s3_bucket: str) -> ObservabilityManager:
    log_group = os.environ.get(
        "CLOUDWATCH_LOG_GROUP", f"/ecs/{environment}-workflow-agent"
    )
    return ObservabilityManager(
        ObservabilityConfig(
            cloudwatch_log_group=log_group,
            s3_bucket=s3_bucket,
            capture_screenshots=_env_flag("CAPTURE_SCREENSHOTS", True),
            # The shipped template creates the log group, so the task role
            # does not need logs:CreateLogGroup.
            create_log_group=False,
        )
    )


def _make_agent(
    payload: Dict[str, Any], api_key: str, environment: str, s3_bucket: str
) -> WorkflowAgent:
    agent_config = AgentConfig(
        starting_url=payload["starting_url"],
        api_key=api_key,
        allowed_domains=payload["allowed_domains"],
        blocked_domains=payload.get("blocked_domains", []),
        headless=True,
    )
    return WorkflowAgent(
        config=agent_config,
        observability_manager=_make_observability(environment, s3_bucket),
    )


def _build_checkout_flow(payload: Dict[str, Any]) -> CheckoutFlowDefinition:
    return CheckoutFlowDefinition(
        flow_id=payload["task_id"],
        starting_url=payload["starting_url"],
        step_wait_seconds=payload.get("step_wait_seconds", 2.0),
        steps=[
            CheckoutStep(
                step_index=step.get("step_index", index),
                step_name=step.get("step_name", f"step_{index}"),
                prompt=step["prompt"],
                expected_url_pattern=step.get("expected_url_pattern"),
            )
            for index, step in enumerate(payload["steps"])
        ],
    )


def _build_qa_test(payload: Dict[str, Any]) -> QATestDefinition:
    return QATestDefinition(
        test_id=payload["task_id"],
        test_name=payload.get("test_name", payload["task_id"]),
        starting_url=payload["starting_url"],
        steps=[
            QATestStep(
                step_index=step.get("step_index", index),
                action_prompt=step["action_prompt"],
                expected_outcome=step["expected_outcome"],
            )
            for index, step in enumerate(payload["steps"])
        ],
    )


def _run_single(payload: Dict[str, Any], api_key: str, environment: str, s3_bucket: str) -> int:
    task_type = payload.get("task_type", "custom")
    try:
        agent = _make_agent(payload, api_key, environment, s3_bucket)
        if task_type == "checkout_flow":
            flow = _build_checkout_flow(payload)
        elif task_type == "qa_test":
            qa_test = _build_qa_test(payload)
        else:
            task = _build_task(payload)
    except KeyError as exc:
        print(f"ERROR: missing required payload field {exc}", file=sys.stderr)
        return 1
    except (TypeError, ValidationError) as exc:
        print(f"ERROR: invalid payload: {exc}", file=sys.stderr)
        return 1

    try:
        if task_type == "checkout_flow":
            result = agent.run_checkout_flow(flow)
        elif task_type == "qa_test":
            result = agent.run_qa_test(qa_test)
        else:
            result = agent.execute_task(task)
    except AgentSessionError as exc:
        # Pre-flight rejection (prompt filter, domain check) or SDK missing.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(result.model_dump_json())
    return 0


def _run_batch(payload: Dict[str, Any], api_key: str, environment: str, s3_bucket: str) -> int:
    raw_tasks = payload.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        print("ERROR: 'tasks' must be a non-empty array for batch payloads.", file=sys.stderr)
        return 1

    # The shared agent config takes its allowed_domains from the union of every
    # task's domains so that the security check accepts each starting URL.
    # Caller can override via top-level fields if it wants tighter scoping.
    starting_url = payload.get("starting_url") or raw_tasks[0]["starting_url"]
    allowed_domains = payload.get("allowed_domains") or _union_domains(raw_tasks)
    if not allowed_domains:
        print("ERROR: no allowed_domains found in payload or tasks.", file=sys.stderr)
        return 1

    agent_config = AgentConfig(
        starting_url=starting_url,
        api_key=api_key,
        allowed_domains=allowed_domains,
        headless=True,
    )

    fleet_config = FleetConfig(
        concurrency_limit=int(payload.get("concurrency_limit", 5)),
        agent_config=agent_config,
        deployment_pattern=DeploymentPattern.ECS,
        # HITL escalation is wired up only when the SNS_TOPIC_ARN env var is
        # set. Without it, failed tasks are reported in the BatchResult but
        # not published anywhere.
        hitl_enabled=bool(os.environ.get("SNS_TOPIC_ARN", "").strip()),
    )

    observability = _make_observability(environment, s3_bucket)
    orchestrator = FleetOrchestrator(config=fleet_config)
    orchestrator.set_observability_manager(observability)

    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN", "").strip()
    if sns_topic_arn:
        orchestrator.set_hitl_manager(
            HITLManager(HITLConfig(sns_topic_arn=sns_topic_arn))
        )

    # Replace the orchestrator's default WorkflowAgent so it shares the same
    # observability pipeline (otherwise it would use the default group).
    orchestrator.set_workflow_agent(
        WorkflowAgent(config=agent_config, observability_manager=observability)
    )

    try:
        tasks = [_build_task(task_payload) for task_payload in raw_tasks]
    except KeyError as exc:
        print(f"ERROR: missing required task field {exc}", file=sys.stderr)
        return 1

    batch_result = orchestrator.submit_batch(tasks)
    print(batch_result.model_dump_json())
    return 0


def main() -> int:
    try:
        _check_region()
        payload = _read_payload()
        api_key = _require("NOVA_ACT_API_KEY")
        s3_bucket = _require("S3_ARTIFACT_BUCKET")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    environment = os.environ.get("ENVIRONMENT", "nova-act").strip() or "nova-act"

    if "tasks" in payload:
        if any(t.get("task_type") in MULTI_STEP_TASK_TYPES for t in payload.get("tasks") or []):
            print(
                "ERROR: batch payloads support single-prompt tasks only; submit "
                "checkout_flow and qa_test payloads as individual tasks.",
                file=sys.stderr,
            )
            return 1
        return _run_batch(payload, api_key, environment, s3_bucket)
    return _run_single(payload, api_key, environment, s3_bucket)


if __name__ == "__main__":
    sys.exit(main())
