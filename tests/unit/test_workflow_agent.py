"""Unit tests for WorkflowAgent core (Task 7.1).

Tests agent initialization, domain validation rejection, session timeout,
prompt length validation, and screenshot/log delegation.
"""

import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from nova_act_fleet.components.security_manager import SecurityManager
from nova_act_fleet.components.workflow_agent import AgentSessionError, WorkflowAgent
from nova_act_fleet.models.config import AgentConfig, ObservabilityConfig, SecurityConfig
from nova_act_fleet.models.domain import LogEvent
from nova_act_fleet.models.tasks import TaskDefinition, TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> AgentConfig:
    defaults = dict(
        starting_url="https://example.com",
        api_key="test-key",
        allowed_domains=["example.com"],
        blocked_domains=[],
        headless=True,
        session_timeout_seconds=1800,
        max_steps=100,
        max_prompt_length=10000,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_task(**overrides) -> TaskDefinition:
    defaults = dict(
        task_id="task-1",
        task_type="custom",
        starting_url="https://example.com",
        prompt="Click the login button",
    )
    defaults.update(overrides)
    return TaskDefinition(**defaults)


def _make_mock_observability():
    """Return a mock ObservabilityManager that doesn't call AWS."""
    mock = MagicMock()
    mock.log_event = MagicMock()
    mock.store_artifact = MagicMock(return_value="nova-act-artifacts/screenshots/test.png")
    return mock


# ---------------------------------------------------------------------------
# __init__ tests
# ---------------------------------------------------------------------------

class TestWorkflowAgentInit:
    """Tests for WorkflowAgent.__init__."""

    @patch("nova_act_fleet.components.workflow_agent.ObservabilityManager")
    def test_creates_default_security_and_observability(self, mock_obs_cls):
        """When no managers are provided, __init__ creates them internally."""
        config = _make_config()
        agent = WorkflowAgent(config)

        assert agent._config is config
        assert isinstance(agent._security, SecurityManager)
        assert agent._nova is None
        assert agent._session_timed_out is False

    def test_accepts_injected_security_manager(self):
        """When a SecurityManager is provided, it is used directly."""
        config = _make_config()
        sec = SecurityManager(SecurityConfig(allowed_domains=["injected.com"]))
        obs = _make_mock_observability()

        agent = WorkflowAgent(config, security_manager=sec, observability_manager=obs)

        assert agent._security is sec

    def test_accepts_injected_observability_manager(self):
        """When an ObservabilityManager is provided, it is used directly."""
        config = _make_config()
        obs = _make_mock_observability()

        agent = WorkflowAgent(config, observability_manager=obs)

        assert agent._observability is obs


# ---------------------------------------------------------------------------
# _validate_domain tests
# ---------------------------------------------------------------------------

class TestValidateDomain:
    """Tests for WorkflowAgent._validate_domain."""

    def test_allowed_domain_returns_true(self):
        config = _make_config(allowed_domains=["example.com"])
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        assert agent._validate_domain("https://example.com/page") is True

    def test_blocked_domain_returns_false(self):
        config = _make_config(
            allowed_domains=["example.com", "evil.com"],
            blocked_domains=["evil.com"],
        )
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        assert agent._validate_domain("https://evil.com/page") is False

    def test_unknown_domain_returns_false(self):
        config = _make_config(allowed_domains=["example.com"])
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        assert agent._validate_domain("https://unknown.com") is False


# ---------------------------------------------------------------------------
# _emit_log tests
# ---------------------------------------------------------------------------

class TestEmitLog:
    """Tests for WorkflowAgent._emit_log."""

    def test_delegates_to_observability_manager(self):
        config = _make_config()
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        event = LogEvent(
            timestamp=datetime.now(timezone.utc),
            task_id="t1",
            event_type="task_start",
            message="hello",
        )
        agent._emit_log(event)

        obs.log_event.assert_called_once_with(event)

    def test_swallows_logging_exceptions(self):
        config = _make_config()
        obs = _make_mock_observability()
        obs.log_event.side_effect = RuntimeError("CloudWatch down")
        agent = WorkflowAgent(config, observability_manager=obs)

        event = LogEvent(
            timestamp=datetime.now(timezone.utc),
            task_id="t1",
            event_type="error",
            message="boom",
        )
        # Should not raise
        agent._emit_log(event)


# ---------------------------------------------------------------------------
# _capture_screenshot tests
# ---------------------------------------------------------------------------

class TestCaptureScreenshot:
    """Tests for WorkflowAgent._capture_screenshot."""

    def test_stores_screenshot_via_observability(self):
        config = _make_config()
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        # Simulate an active nova session with a page that returns bytes
        mock_nova = MagicMock()
        mock_nova.page.screenshot.return_value = b"\x89PNG"
        agent._nova = mock_nova

        key = agent._capture_screenshot()

        assert key == "nova-act-artifacts/screenshots/test.png"
        obs.store_artifact.assert_called_once()
        call_args = obs.store_artifact.call_args
        assert call_args[0][0] == b"\x89PNG"  # artifact bytes

    def test_returns_key_even_when_screenshot_fails(self):
        config = _make_config()
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        # No active session
        agent._nova = None

        key = agent._capture_screenshot()

        # Should still store (empty bytes) and return key
        obs.store_artifact.assert_called_once()


# ---------------------------------------------------------------------------
# execute_task tests
# ---------------------------------------------------------------------------

class TestExecuteTask:
    """Tests for WorkflowAgent.execute_task."""

    def test_rejects_prompt_exceeding_max_length(self):
        """Prompt longer than max_prompt_length returns REJECTED status."""
        config = _make_config(max_prompt_length=50)
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        task = _make_task(prompt="x" * 51)
        result = agent.execute_task(task)

        assert result.status == TaskStatus.REJECTED
        assert result.error_details is not None
        assert len(result.error_details) == 1
        assert "exceeds maximum" in result.error_details[0].message

    def test_raises_on_domain_validation_failure(self):
        """Starting URL not in allowlist raises AgentSessionError."""
        config = _make_config(allowed_domains=["example.com"])
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        task = _make_task(starting_url="https://evil.com")

        with pytest.raises(AgentSessionError, match="Domain validation failed"):
            agent.execute_task(task)

    def test_raises_when_nova_act_not_installed(self):
        """When NovaAct is None, raises AgentSessionError."""
        config = _make_config()
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        task = _make_task()

        with patch("nova_act_fleet.components.workflow_agent.NovaAct", None):
            with pytest.raises(AgentSessionError, match="not installed"):
                agent.execute_task(task)

    def test_successful_execution_returns_success(self):
        """Happy path: NovaAct session succeeds, returns SUCCESS."""
        config = _make_config()
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        task = _make_task()

        mock_nova_instance = MagicMock()
        mock_nova_instance.act.return_value = {"clicked": True}
        mock_nova_instance.page.screenshot.return_value = b"\x89PNG"

        mock_nova_ctx = MagicMock()
        mock_nova_ctx.__enter__ = MagicMock(return_value=mock_nova_instance)
        mock_nova_ctx.__exit__ = MagicMock(return_value=False)

        with patch("nova_act_fleet.components.workflow_agent.NovaAct", return_value=mock_nova_ctx):
            result = agent.execute_task(task)

        assert result.status == TaskStatus.SUCCESS
        assert result.task_id == "task-1"
        assert result.output == {"clicked": True}
        assert result.page_url == "https://example.com"

    def test_act_error_returns_failed(self):
        """When nova.act() raises, returns FAILED with error details."""
        config = _make_config()
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        task = _make_task()

        mock_nova_instance = MagicMock()
        mock_nova_instance.act.side_effect = RuntimeError("Element not found")

        mock_nova_ctx = MagicMock()
        mock_nova_ctx.__enter__ = MagicMock(return_value=mock_nova_instance)
        mock_nova_ctx.__exit__ = MagicMock(return_value=False)

        with patch("nova_act_fleet.components.workflow_agent.NovaAct", return_value=mock_nova_ctx):
            result = agent.execute_task(task)

        assert result.status == TaskStatus.FAILED
        assert result.error_details is not None
        assert len(result.error_details) == 1
        assert result.error_details[0].error_type == "RuntimeError"
        assert "Element not found" in result.error_details[0].message

    def test_timeout_returns_timeout_status(self):
        """When session times out, returns TIMEOUT status."""
        config = _make_config(session_timeout_seconds=1800)
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        task = _make_task()

        mock_nova_instance = MagicMock()

        def act_with_timeout(*args, **kwargs):
            # Simulate the timeout flag being set during execution
            agent._session_timed_out = True
            return {"result": "partial"}

        mock_nova_instance.act.side_effect = act_with_timeout

        mock_nova_ctx = MagicMock()
        mock_nova_ctx.__enter__ = MagicMock(return_value=mock_nova_instance)
        mock_nova_ctx.__exit__ = MagicMock(return_value=False)

        with patch("nova_act_fleet.components.workflow_agent.NovaAct", return_value=mock_nova_ctx):
            result = agent.execute_task(task)

        assert result.status == TaskStatus.TIMEOUT

    def test_passes_api_key_to_nova_act(self):
        """NovaAct is constructed with the api_key from config and without
        the obsolete ``allowed_domains`` kwarg (domain enforcement now lives
        in SecurityManager.validate_domain, not the SDK)."""
        config = _make_config(allowed_domains=["example.com", "test.com"])
        obs = _make_mock_observability()
        agent = WorkflowAgent(config, observability_manager=obs)

        task = _make_task()

        mock_nova_instance = MagicMock()
        mock_nova_instance.act.return_value = "ok"
        mock_nova_instance.page.screenshot.return_value = b""

        mock_nova_ctx = MagicMock()
        mock_nova_ctx.__enter__ = MagicMock(return_value=mock_nova_instance)
        mock_nova_ctx.__exit__ = MagicMock(return_value=False)

        with patch("nova_act_fleet.components.workflow_agent.NovaAct", return_value=mock_nova_ctx) as mock_cls:
            agent.execute_task(task)

        mock_cls.assert_called_once_with(
            starting_page="https://example.com",
            headless=True,
            nova_act_api_key=config.api_key,
        )
