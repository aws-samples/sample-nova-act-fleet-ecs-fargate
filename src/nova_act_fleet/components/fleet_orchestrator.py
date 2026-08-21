"""FleetOrchestrator for Nova Act Fleet.

Manages parallel execution of WorkflowAgents using ThreadPoolExecutor,
validates task payloads, aggregates results into BatchResult, escalates
failures to HITL queue, and publishes CloudWatch metrics.
"""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List

from nova_act_fleet.components.hitl_manager import HITLManager
from nova_act_fleet.components.observability_manager import ObservabilityManager
from nova_act_fleet.components.workflow_agent import WorkflowAgent
from nova_act_fleet.models.config import FleetConfig, HITLConfig, ObservabilityConfig
from nova_act_fleet.models.domain import LogEvent, MetricData
from nova_act_fleet.models.tasks import (
    BatchResult,
    ErrorDetail,
    TaskDefinition,
    TaskResult,
    TaskStatus,
)


class FleetOrchestrator:
    """Dispatches tasks to WorkflowAgents and aggregates results.

    Validates each task payload is under 5 MB before dispatch, uses a
    ThreadPoolExecutor with a developer-configured concurrency limit for
    parallel execution, continues processing remaining tasks when
    individual agents fail, escalates failed tasks (after retry
    exhaustion) to the HITL queue via HITLManager, and publishes
    CloudWatch metrics for success rate, duration, and HITL escalation
    rate via ObservabilityManager.
    """

    def __init__(self, config: FleetConfig) -> None:
        self._config = config
        self._workflow_agent = WorkflowAgent(config=config.agent_config)
        self._observability = ObservabilityManager(
            ObservabilityConfig(
                cloudwatch_log_group="/nova-act-fleet/orchestrator",
                s3_bucket="nova-act-fleet-artifacts",
            )
        )
        self._hitl_manager: HITLManager | None = None
        if config.hitl_enabled:
            # HITLConfig requires an SNS topic ARN; use a placeholder that
            # callers can override by providing their own HITLManager.
            self._hitl_manager = HITLManager(
                HITLConfig(sns_topic_arn="arn:aws:sns:us-east-1:000000000000:nova-act-fleet-hitl")
            )

    # -- public helpers for dependency injection --------------------------

    def set_workflow_agent(self, agent: WorkflowAgent) -> None:
        """Replace the default WorkflowAgent (useful for testing)."""
        self._workflow_agent = agent

    def set_observability_manager(self, manager: ObservabilityManager) -> None:
        """Replace the default ObservabilityManager."""
        self._observability = manager

    def set_hitl_manager(self, manager: HITLManager) -> None:
        """Replace the default HITLManager."""
        self._hitl_manager = manager

    # -- core API ---------------------------------------------------------

    def submit_batch(self, tasks: List[TaskDefinition]) -> BatchResult:
        """Validate payloads, dispatch tasks in parallel, and aggregate results.

        Each task's JSON-serialized size is checked against the configured
        maximum (default 5 MB). Oversized tasks are immediately rejected.
        Valid tasks are dispatched via a ThreadPoolExecutor whose
        ``max_workers`` equals ``config.concurrency_limit``.

        After all tasks complete, failed tasks that exhausted retries are
        escalated to the HITL queue (if enabled), batch metrics are
        published, and a BatchResult is returned.

        Args:
            tasks: List of task definitions to execute.

        Returns:
            A BatchResult aggregating per-task results and batch-level
            statistics.
        """
        batch_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        results: List[TaskResult] = []

        # Separate valid and oversized tasks
        valid_tasks: List[TaskDefinition] = []
        for task in tasks:
            if not self._validate_payload_size(task):
                now = datetime.now(timezone.utc)
                results.append(
                    TaskResult(
                        task_id=task.task_id,
                        status=TaskStatus.REJECTED,
                        started_at=now,
                        completed_at=now,
                        duration_seconds=0.0,
                        error_details=[
                            ErrorDetail(
                                error_type="PayloadSizeExceeded",
                                message=(
                                    f"Task payload size exceeds maximum "
                                    f"{self._config.max_payload_size_bytes} bytes"
                                ),
                                timestamp=now,
                            )
                        ],
                    )
                )
            else:
                valid_tasks.append(task)

        # Dispatch valid tasks in parallel
        if valid_tasks:
            with ThreadPoolExecutor(max_workers=self._config.concurrency_limit) as pool:
                future_to_task = {
                    pool.submit(self._dispatch_task, t): t for t in valid_tasks
                }
                for future in as_completed(future_to_task):
                    results.append(future.result())

        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()

        # Compute aggregation counts
        total = len(tasks)
        succeeded = sum(1 for r in results if r.status == TaskStatus.SUCCESS)
        failed_count = sum(1 for r in results if r.status == TaskStatus.FAILED)
        rejected = sum(1 for r in results if r.status == TaskStatus.REJECTED)

        # Escalate failed tasks to HITL
        escalated = 0
        for result in results:
            if result.status == TaskStatus.FAILED:
                task_def = next((t for t in tasks if t.task_id == result.task_id), None)
                if task_def is not None and result.retry_count >= task_def.max_retries:
                    self._escalate_to_hitl(task_def, result)
                    escalated += 1

        # Adjust counts: escalated tasks move from failed to escalated
        failed_count -= escalated

        success_rate = succeeded / total if total > 0 else 0.0
        hitl_escalation_rate = escalated / total if total > 0 else 0.0

        batch_result = BatchResult(
            batch_id=batch_id,
            total_tasks=total,
            succeeded=succeeded,
            failed=failed_count,
            escalated=escalated,
            rejected=rejected,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
            success_rate=success_rate,
            hitl_escalation_rate=hitl_escalation_rate,
            task_results=results,
        )

        self._publish_metrics(batch_result)
        return batch_result

    # -- internal helpers -------------------------------------------------

    def _dispatch_task(self, task: TaskDefinition) -> TaskResult:
        """Execute a single task via WorkflowAgent, handling errors gracefully.

        If the WorkflowAgent raises an unexpected exception, it is caught
        and wrapped in a FAILED TaskResult so that the batch continues.

        Args:
            task: The task definition to execute.

        Returns:
            A TaskResult from the WorkflowAgent or a synthetic failure result.
        """
        if self._hitl_manager is not None:
            self._hitl_manager.record_task()

        try:
            return self._workflow_agent.execute_task(task)
        except Exception as exc:
            now = datetime.now(timezone.utc)
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                started_at=now,
                completed_at=now,
                duration_seconds=0.0,
                error_details=[
                    ErrorDetail(
                        error_type=type(exc).__name__,
                        message=str(exc),
                        page_url=task.starting_url,
                        timestamp=now,
                    )
                ],
            )

    def _validate_payload_size(self, task: TaskDefinition) -> bool:
        """Check whether a task's JSON-serialized size is within limits.

        Args:
            task: The task definition to validate.

        Returns:
            True if the serialized size is ≤ ``max_payload_size_bytes``,
            False otherwise.
        """
        serialized = task.model_dump_json()
        return len(serialized.encode("utf-8")) <= self._config.max_payload_size_bytes

    def _escalate_to_hitl(self, task: TaskDefinition, result: TaskResult) -> None:
        """Delegate escalation of a failed task to HITLManager.

        Extracts the error history and last screenshot key from the
        TaskResult and forwards them to ``HITLManager.escalate()``.

        Args:
            task: The original task definition.
            result: The failed task result containing error details.
        """
        if self._hitl_manager is None:
            return

        error_history = result.error_details or []
        screenshot_key = result.screenshot_keys[-1] if result.screenshot_keys else ""

        self._hitl_manager.escalate(
            task=task,
            error_history=error_history,
            screenshot_key=screenshot_key,
        )

    def _publish_metrics(self, batch_result: BatchResult) -> None:
        """Publish batch-level CloudWatch metrics via ObservabilityManager.

        Emits three metrics:
        - TaskSuccessRate (Percent)
        - AverageTaskDuration (Seconds)
        - HITLEscalationRate (Percent)

        Args:
            batch_result: The completed batch result to derive metrics from.
        """
        # Success rate
        self._observability.publish_metric(
            MetricData(
                metric_name="TaskSuccessRate",
                value=batch_result.success_rate,
                unit="Percent",
            )
        )

        # Average task duration
        durations = [r.duration_seconds for r in batch_result.task_results]
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        self._observability.publish_metric(
            MetricData(
                metric_name="AverageTaskDuration",
                value=avg_duration,
                unit="Seconds",
            )
        )

        # HITL escalation rate
        self._observability.publish_metric(
            MetricData(
                metric_name="HITLEscalationRate",
                value=batch_result.hitl_escalation_rate,
                unit="Percent",
            )
        )
