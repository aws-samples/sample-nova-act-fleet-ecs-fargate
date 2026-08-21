"""Domain-specific models for Nova Act Fleet.

Defines structured output, checkout flow, QA testing, observability,
prompt filtering, and HITL resolution data models.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from nova_act_fleet.models.tasks import ErrorDetail, TaskStatus


class StructuredOutput(BaseModel):
    """Wrapper for extracted structured data.

    Represents validated data extracted from a web page via a Pydantic
    model schema, with an optional extraction confidence score.
    """

    task_id: str
    schema_name: str
    data: Dict[str, Any]
    extraction_confidence: Optional[float] = None


class CheckoutStep(BaseModel):
    """A single step in a checkout flow.

    Each step carries a natural language prompt (capped at 10,000 chars)
    and an optional URL pattern for post-step validation.
    """

    step_index: int
    step_name: str  # "cart_review", "shipping", "payment", "confirmation"
    prompt: str = Field(max_length=10000)
    expected_url_pattern: Optional[str] = None


class CheckoutFlowDefinition(BaseModel):
    """Defines a multi-step checkout flow.

    Contains an ordered list of CheckoutSteps with a configurable
    wait time between steps to accommodate page load variability.
    """

    flow_id: str
    starting_url: str
    steps: List[CheckoutStep]
    step_wait_seconds: float = Field(default=2.0, ge=0)


class StepResult(BaseModel):
    """Result of a single checkout step."""

    step_index: int
    step_name: str
    status: TaskStatus
    screenshot_key: Optional[str] = None
    page_url: Optional[str] = None
    error: Optional[ErrorDetail] = None


class CheckoutResult(BaseModel):
    """Result of a checkout flow execution.

    Tracks how many steps completed, individual step results,
    and the final screenshot captured.
    """

    flow_id: str
    status: TaskStatus
    completed_steps: int
    total_steps: int
    step_results: List[StepResult]
    final_screenshot_key: Optional[str] = None


class QATestStep(BaseModel):
    """A single QA test step.

    Pairs a natural language action prompt with the expected outcome
    string for comparison after execution.
    """

    step_index: int
    action_prompt: str = Field(max_length=10000)
    expected_outcome: str


class QATestDefinition(BaseModel):
    """Defines a QA test with natural language steps."""

    test_id: str
    test_name: str
    starting_url: str
    steps: List[QATestStep]


class QAStepReport(BaseModel):
    """Report for a single QA test step.

    Contains pass/fail status, expected vs actual outcome,
    screenshot evidence, and step duration.
    """

    step_index: int
    status: TaskStatus
    expected_outcome: str
    actual_outcome: Optional[str] = None
    screenshot_key: Optional[str] = None
    duration_seconds: float


class QATestReport(BaseModel):
    """Report from a QA test execution.

    Aggregates per-step reports with overall pass/fail status,
    step counts, and total duration.
    """

    test_id: str
    test_name: str
    status: TaskStatus  # SUCCESS if all steps pass, FAILED otherwise
    total_steps: int
    passed_steps: int
    failed_steps: int
    duration_seconds: float
    step_reports: List[QAStepReport]


class LogEvent(BaseModel):
    """Structured log event emitted to CloudWatch Logs.

    Every task execution emits events with timestamp, task ID,
    event type, optional status, message, and arbitrary metadata.
    """

    timestamp: datetime
    task_id: str
    event_type: str  # "task_start", "task_end", "step_complete", "error", "escalation"
    status: Optional[TaskStatus] = None
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MetricData(BaseModel):
    """CloudWatch metric data point.

    Represents a single metric value with its unit and optional
    dimensions for the NovaActFleet namespace.
    """

    metric_name: str
    value: float
    unit: str  # "Count", "Seconds", "Percent"
    dimensions: Dict[str, str] = Field(default_factory=dict)


class PromptFilterResult(BaseModel):
    """Result of prompt input filtering.

    Indicates whether a prompt was allowed or blocked, with a
    confidence score between 0.0 and 1.0.
    """

    allowed: bool
    blocked_reason: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)


class HITLResolution(BaseModel):
    """Human resolution of an escalated task.

    Records who resolved the escalation, the outcome status,
    optional notes, and the resolution timestamp.
    """

    escalation_id: str
    resolved_by: str
    resolution_status: str  # "completed", "skipped", "reassigned"
    resolution_notes: Optional[str] = None
    resolved_at: datetime
