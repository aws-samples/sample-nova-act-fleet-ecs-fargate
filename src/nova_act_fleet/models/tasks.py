"""Task and result models for Nova Act Fleet.

Defines task lifecycle status, task definitions with prompt and retry constraints,
per-task execution results with error details, and batch-level aggregation.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Lifecycle status of a task execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ESCALATED = "escalated"
    TIMEOUT = "timeout"
    REJECTED = "rejected"


class TaskDefinition(BaseModel):
    """Defines a single automation task.

    Enforces prompt length and retry constraints as required by the design spec.
    """

    task_id: str
    task_type: str  # "form", "extraction", "checkout", "qa_test", "custom"
    starting_url: str
    prompt: str = Field(max_length=10000)
    input_data: Optional[Dict[str, Any]] = None
    max_retries: int = Field(default=3, ge=0)
    step_wait_seconds: float = Field(default=1.0, ge=0)
    return_type_schema: Optional[Dict[str, Any]] = None  # JSON Schema for Pydantic model


class ErrorDetail(BaseModel):
    """Detailed error information from a task execution."""

    error_type: str
    message: str
    step_index: Optional[int] = None
    page_url: Optional[str] = None
    screenshot_key: Optional[str] = None
    timestamp: datetime


class TaskResult(BaseModel):
    """Result of a single task execution."""

    task_id: str
    status: TaskStatus
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    output: Optional[Any] = None
    error_details: Optional[List[ErrorDetail]] = None
    screenshot_keys: List[str] = Field(default_factory=list)
    page_url: Optional[str] = None
    retry_count: int = 0


class BatchResult(BaseModel):
    """Aggregated result of a batch of tasks."""

    batch_id: str
    total_tasks: int
    succeeded: int
    failed: int
    escalated: int
    rejected: int
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    success_rate: float
    hitl_escalation_rate: float
    task_results: List[TaskResult]
