"""Configuration models for Nova Act Fleet.

Defines deployment patterns, agent configuration, fleet orchestration settings,
security controls, observability parameters, and HITL escalation configuration.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class DeploymentPattern(str, Enum):
    """Supported AWS deployment patterns for Workflow Agents.

    Only ECS is shipped in v0.2 — see ``templates/ecs_deployment.yaml`` and
    ``docs/deployment_guide.md``. LAMBDA and AGENTCORE are preserved on the
    enum for forward compatibility:

    - LAMBDA was evaluated and intentionally cut from v0.2; the rationale
      and the outline a future contributor would need are in
      ``docs/decision_matrix.md`` § "Why no Lambda template?".
    - AGENTCORE is out of scope by design; see ``docs/why_not_agentcore.md``.
      If AgentCore Browser fits your environment, use it directly via the
      Nova Act + AgentCore Browser quickstart instead of this project.

    ``SecurityManager.get_iam_policy(...)`` returns starter IAM policies for
    all three values, so callers who later move to LAMBDA or AGENTCORE have
    a stable API surface; the policies are starting points, not deployable
    IAM policies in this project.
    """

    LAMBDA = "lambda"
    ECS = "ecs"
    AGENTCORE = "agentcore"


class AgentConfig(BaseModel):
    """Configuration for a single WorkflowAgent.

    Enforces upper-bound constraints on session duration, step count,
    and prompt length as required by the design specification.
    """

    starting_url: str
    api_key: Optional[str] = None
    allowed_domains: List[str] = Field(min_length=1)
    blocked_domains: List[str] = Field(default_factory=list)
    headless: bool = True
    session_timeout_seconds: int = Field(default=1800, le=1800)  # Max 30 min
    max_steps: int = Field(default=100, le=100)
    max_prompt_length: int = Field(default=10000, le=10000)


class FleetConfig(BaseModel):
    """Configuration for the FleetOrchestrator.

    Controls concurrency, payload limits, deployment pattern selection,
    and HITL escalation toggle.
    """

    concurrency_limit: int = Field(default=10, ge=1)
    max_payload_size_bytes: int = Field(default=5_242_880)  # 5 MB
    agent_config: AgentConfig
    deployment_pattern: DeploymentPattern
    hitl_enabled: bool = True


class SecurityConfig(BaseModel):
    """Configuration for the SecurityManager.

    Defines domain restrictions, tool registration, file access control,
    and input filtering patterns for the six-layer security architecture.
    """

    allowed_domains: List[str]
    blocked_domains: List[str] = Field(default_factory=list)
    registered_tools: List[str] = Field(default_factory=list)
    file_access_enabled: bool = False  # Blocked by default
    input_filter_patterns: List[str] = Field(default_factory=list)


class ObservabilityConfig(BaseModel):
    """Configuration for the ObservabilityManager.

    Specifies CloudWatch, S3, and alarm settings for centralized
    monitoring and artifact retention.
    """

    cloudwatch_log_group: str
    s3_bucket: str
    s3_prefix: str = "nova-act-artifacts"
    artifact_retention_days: int = Field(default=90, ge=1)
    metric_namespace: str = "NovaActFleet"
    alarm_success_rate_threshold: float = Field(default=0.90)
    alarm_evaluation_period_minutes: int = Field(default=15)
    alarm_sns_topic_arn: Optional[str] = None
    """Optional SNS topic ARN used as the AlarmActions/OKActions target when
    alarms are created imperatively via ``ObservabilityManager.configure_alarms``.
    Deployments that use ``templates/ecs_deployment.yaml`` do not need to set
    this; the CFN template creates the alarm and wires the topic directly."""


class HITLConfig(BaseModel):
    """Configuration for the HITLManager.

    Defines SNS topic, optional DynamoDB table, and escalation rate
    target for human-in-the-loop workflows.
    """

    sns_topic_arn: str
    dynamodb_table_name: Optional[str] = None
    escalation_rate_target: float = Field(default=0.05, le=1.0)
