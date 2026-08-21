"""ObservabilityManager for Nova Act Fleet.

Handles structured logging to CloudWatch Logs, custom metric publishing
to CloudWatch Metrics, artifact storage in S3, and CloudWatch alarm
configuration for fleet health monitoring.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3

from nova_act_fleet.models.config import ObservabilityConfig
from nova_act_fleet.models.domain import LogEvent, MetricData


class ObservabilityManager:
    """Centralized observability for agent fleet operations.

    Emits structured JSON logs, publishes CloudWatch metrics under
    the configured namespace, stores artifacts to S3, and configures
    CloudWatch alarms based on the provided ObservabilityConfig.
    """

    def __init__(self, config: ObservabilityConfig) -> None:
        self._config = config
        self._logs_client = boto3.client("logs")
        self._cloudwatch_client = boto3.client("cloudwatch")
        self._s3_client = boto3.client("s3")
        self._log_stream_name: Optional[str] = None

    def _ensure_log_stream(self) -> str:
        """Ensure a CloudWatch Logs log stream exists for the current session.

        Creates the log group and a timestamped log stream if they
        do not already exist.

        Returns:
            The name of the log stream.
        """
        if self._log_stream_name is not None:
            return self._log_stream_name

        # Create log group if it doesn't exist
        try:
            self._logs_client.create_log_group(
                logGroupName=self._config.cloudwatch_log_group
            )
        except self._logs_client.exceptions.ResourceAlreadyExistsException:
            pass

        # Create a unique log stream
        stream_name = f"nova-act-fleet-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        try:
            self._logs_client.create_log_stream(
                logGroupName=self._config.cloudwatch_log_group,
                logStreamName=stream_name,
            )
        except self._logs_client.exceptions.ResourceAlreadyExistsException:
            pass

        self._log_stream_name = stream_name
        return stream_name

    def log_event(self, event: LogEvent) -> None:
        """Emit a structured JSON log event to CloudWatch Logs.

        Serializes the LogEvent to JSON and writes it as a single
        log entry to the configured CloudWatch Logs log group.

        Args:
            event: The structured log event containing task ID,
                   timestamp, event type, status, message, and metadata.
        """
        stream_name = self._ensure_log_stream()

        log_entry: Dict[str, Any] = {
            "timestamp": event.timestamp.isoformat(),
            "task_id": event.task_id,
            "event_type": event.event_type,
            "message": event.message,
            "metadata": event.metadata,
        }
        if event.status is not None:
            log_entry["status"] = event.status.value

        self._logs_client.put_log_events(
            logGroupName=self._config.cloudwatch_log_group,
            logStreamName=stream_name,
            logEvents=[
                {
                    "timestamp": int(event.timestamp.timestamp() * 1000),
                    "message": json.dumps(log_entry, default=str),
                }
            ],
        )

    def publish_metric(self, metric: MetricData) -> None:
        """Publish a custom CloudWatch metric under the configured namespace.

        Sends a single metric data point to CloudWatch Metrics with
        the specified name, value, unit, and optional dimensions.

        Args:
            metric: The metric data containing name, value, unit,
                    and optional dimensions.
        """
        dimensions = [
            {"Name": k, "Value": v} for k, v in metric.dimensions.items()
        ]

        self._cloudwatch_client.put_metric_data(
            Namespace=self._config.metric_namespace,
            MetricData=[
                {
                    "MetricName": metric.metric_name,
                    "Value": metric.value,
                    "Unit": metric.unit,
                    "Dimensions": dimensions,
                    "Timestamp": datetime.now(timezone.utc),
                }
            ],
        )

    def store_artifact(self, artifact: bytes, key: str, metadata: dict) -> str:
        """Upload an artifact to S3 with metadata.

        Stores screenshots, extracted data, or error reports to the
        configured S3 bucket under the configured prefix. Returns
        the full S3 key of the stored object.

        Args:
            artifact: The raw bytes of the artifact to store.
            key: The object key (relative to the S3 prefix).
            metadata: Arbitrary string metadata to attach to the S3 object.

        Returns:
            The full S3 key where the artifact was stored.
        """
        full_key = f"{self._config.s3_prefix}/{key}"

        # Ensure all metadata values are strings for S3
        str_metadata = {str(k): str(v) for k, v in metadata.items()}

        self._s3_client.put_object(
            Bucket=self._config.s3_bucket,
            Key=full_key,
            Body=artifact,
            Metadata=str_metadata,
        )

        return full_key

    def configure_alarms(self) -> None:
        """Create a CloudWatch alarm for low task success rate.

        Configures an alarm that triggers when the TaskSuccessRate
        metric drops below the configured threshold (default 90%)
        over the configured evaluation period (default 15 minutes).

        When ``ObservabilityConfig.alarm_sns_topic_arn`` is set, the topic
        is attached as both ``AlarmActions`` and ``OKActions`` so that
        notifications fire on breach and recovery. Deployments using
        ``templates/ecs_deployment.yaml`` create the equivalent alarm and
        SNS wiring at stack-creation time, so calling this method is only
        needed for imperative / non-CFN setups.
        """
        threshold = self._config.alarm_success_rate_threshold * 100
        period_seconds = self._config.alarm_evaluation_period_minutes * 60

        alarm_kwargs: Dict[str, Any] = {
            "AlarmName": f"{self._config.metric_namespace}-LowSuccessRate",
            "AlarmDescription": (
                f"Task success rate dropped below "
                f"{self._config.alarm_success_rate_threshold:.0%} "
                f"over {self._config.alarm_evaluation_period_minutes} minutes"
            ),
            "Namespace": self._config.metric_namespace,
            "MetricName": "TaskSuccessRate",
            "Statistic": "Average",
            "Period": period_seconds,
            "EvaluationPeriods": 1,
            "Threshold": threshold,
            "ComparisonOperator": "LessThanThreshold",
            "TreatMissingData": "notBreaching",
        }

        if self._config.alarm_sns_topic_arn:
            alarm_kwargs["AlarmActions"] = [self._config.alarm_sns_topic_arn]
            alarm_kwargs["OKActions"] = [self._config.alarm_sns_topic_arn]

        self._cloudwatch_client.put_metric_alarm(**alarm_kwargs)
