"""HITLManager for Nova Act Fleet.

Manages human-in-the-loop escalation workflows: publishes failed tasks
to an SNS topic, optionally tracks escalation state in DynamoDB, records
human resolutions, and computes the current escalation rate.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

from nova_act_fleet.models.config import HITLConfig
from nova_act_fleet.models.domain import HITLResolution
from nova_act_fleet.models.tasks import ErrorDetail, TaskDefinition


class HITLManager:
    """Routes failed tasks to human operators and records resolutions.

    Publishes escalation payloads to an SNS topic containing the task
    definition, error history, and last screenshot S3 key. Optionally
    writes escalation state to a DynamoDB table for tracking. Tracks
    total tasks and escalations to compute the escalation rate, targeting
    below 5% of total tasks processed.
    """

    def __init__(self, config: HITLConfig) -> None:
        self._config = config
        self._sns_client = boto3.client("sns")
        self._dynamodb_client: Optional[Any] = None
        if config.dynamodb_table_name:
            self._dynamodb_client = boto3.client("dynamodb")

        # Counters for escalation rate tracking
        self._total_tasks: int = 0
        self._total_escalations: int = 0

    def escalate(
        self,
        task: TaskDefinition,
        error_history: List[ErrorDetail],
        screenshot_key: str,
    ) -> str:
        """Publish an escalation payload to SNS and optionally write to DynamoDB.

        Constructs a payload containing the full task definition, all error
        details from retry attempts, and the S3 key of the last captured
        screenshot. Publishes the payload to the configured SNS topic and,
        if a DynamoDB table is configured, writes the escalation record for
        state tracking.

        Args:
            task: The original task definition that failed.
            error_history: Complete list of errors from all retry attempts.
            screenshot_key: S3 key of the last captured screenshot.

        Returns:
            A unique escalation ID for tracking the escalation.
        """
        escalation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        payload: Dict[str, Any] = {
            "escalation_id": escalation_id,
            "task_definition": task.model_dump(mode="json"),
            "error_history": [e.model_dump(mode="json") for e in error_history],
            "screenshot_key": screenshot_key,
            "escalated_at": now.isoformat(),
            "status": "pending",
        }

        # Publish to SNS
        self._sns_client.publish(
            TopicArn=self._config.sns_topic_arn,
            Subject=f"HITL Escalation: {task.task_id}",
            Message=json.dumps(payload, default=str),
            MessageAttributes={
                "escalation_id": {
                    "DataType": "String",
                    "StringValue": escalation_id,
                },
                "task_id": {
                    "DataType": "String",
                    "StringValue": task.task_id,
                },
                "task_type": {
                    "DataType": "String",
                    "StringValue": task.task_type,
                },
            },
        )

        # Optionally write to DynamoDB for tracking
        if self._dynamodb_client and self._config.dynamodb_table_name:
            self._dynamodb_client.put_item(
                TableName=self._config.dynamodb_table_name,
                Item={
                    "escalation_id": {"S": escalation_id},
                    "task_id": {"S": task.task_id},
                    "task_type": {"S": task.task_type},
                    "screenshot_key": {"S": screenshot_key},
                    "error_count": {"N": str(len(error_history))},
                    "escalated_at": {"S": now.isoformat()},
                    "status": {"S": "pending"},
                    "payload": {"S": json.dumps(payload, default=str)},
                },
            )

        self._total_escalations += 1
        return escalation_id

    def record_resolution(
        self, escalation_id: str, resolution: HITLResolution
    ) -> None:
        """Record a human resolution for an escalated task.

        Updates the escalation state in DynamoDB (if configured) with the
        resolution details including who resolved it, the outcome status,
        optional notes, and the resolution timestamp.

        Args:
            escalation_id: The unique ID of the escalation to resolve.
            resolution: The human resolution details.
        """
        if self._dynamodb_client and self._config.dynamodb_table_name:
            self._dynamodb_client.update_item(
                TableName=self._config.dynamodb_table_name,
                Key={"escalation_id": {"S": escalation_id}},
                UpdateExpression=(
                    "SET #s = :status, "
                    "resolved_by = :resolved_by, "
                    "resolution_status = :resolution_status, "
                    "resolution_notes = :resolution_notes, "
                    "resolved_at = :resolved_at"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": {"S": "resolved"},
                    ":resolved_by": {"S": resolution.resolved_by},
                    ":resolution_status": {"S": resolution.resolution_status},
                    ":resolution_notes": {
                        "S": resolution.resolution_notes or ""
                    },
                    ":resolved_at": {"S": resolution.resolved_at.isoformat()},
                },
            )

    def get_escalation_rate(self) -> float:
        """Compute and return the current escalation rate.

        The escalation rate is the ratio of escalated tasks to total tasks
        processed. The target is below 5% (configurable via
        ``HITLConfig.escalation_rate_target``).

        Returns:
            The escalation rate as a float between 0.0 and 1.0.
            Returns 0.0 if no tasks have been processed.
        """
        if self._total_tasks == 0:
            return 0.0
        return self._total_escalations / self._total_tasks

    def record_task(self) -> None:
        """Increment the total task counter.

        Should be called for every task processed by the fleet to
        maintain an accurate escalation rate calculation.
        """
        self._total_tasks += 1
