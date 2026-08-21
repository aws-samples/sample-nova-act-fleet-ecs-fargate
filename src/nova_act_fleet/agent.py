"""Public API module for Nova Act Fleet.

Re-exports the main components and all data models so users can write::

    from nova_act_fleet.agent import WorkflowAgent, FleetOrchestrator
    from nova_act_fleet.agent import AgentConfig, FleetConfig, TaskDefinition
"""

# Core components
from nova_act_fleet.components.fleet_orchestrator import FleetOrchestrator
from nova_act_fleet.components.hitl_manager import HITLManager
from nova_act_fleet.components.observability_manager import ObservabilityManager
from nova_act_fleet.components.security_manager import SecurityManager
from nova_act_fleet.components.workflow_agent import AgentSessionError, WorkflowAgent

# Configuration models
from nova_act_fleet.models.config import (
    AgentConfig,
    DeploymentPattern,
    FleetConfig,
    HITLConfig,
    ObservabilityConfig,
    SecurityConfig,
)

# Domain models
from nova_act_fleet.models.domain import (
    CheckoutFlowDefinition,
    CheckoutResult,
    CheckoutStep,
    HITLResolution,
    LogEvent,
    MetricData,
    PromptFilterResult,
    QAStepReport,
    QATestDefinition,
    QATestReport,
    QATestStep,
    StepResult,
    StructuredOutput,
)

# Task and result models
from nova_act_fleet.models.tasks import (
    BatchResult,
    ErrorDetail,
    TaskDefinition,
    TaskResult,
    TaskStatus,
)

__all__ = [
    # Components
    "WorkflowAgent",
    "FleetOrchestrator",
    "SecurityManager",
    "ObservabilityManager",
    "HITLManager",
    "AgentSessionError",
    # Config models
    "AgentConfig",
    "FleetConfig",
    "SecurityConfig",
    "ObservabilityConfig",
    "HITLConfig",
    "DeploymentPattern",
    # Task models
    "TaskStatus",
    "TaskDefinition",
    "TaskResult",
    "ErrorDetail",
    "BatchResult",
    # Domain models
    "StructuredOutput",
    "CheckoutFlowDefinition",
    "CheckoutStep",
    "CheckoutResult",
    "StepResult",
    "QATestDefinition",
    "QATestStep",
    "QATestReport",
    "QAStepReport",
    "LogEvent",
    "MetricData",
    "PromptFilterResult",
    "HITLResolution",
]
