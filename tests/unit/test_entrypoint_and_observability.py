"""Unit tests for the container entry point and ObservabilityManager.

Covers round-2 review items A-6 (concurrency default), A-8 (Region
fail-fast), A-9 (multi-step payload routing), and the CreateLogGroup
permission gap on the shipped task role.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from nova_act_fleet.components.observability_manager import ObservabilityManager
from nova_act_fleet.models.config import FleetConfig, ObservabilityConfig
from nova_act_fleet.models.domain import CheckoutResult, QATestReport
from nova_act_fleet.models.tasks import TaskStatus

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.py"


@pytest.fixture()
def entrypoint(monkeypatch):
    spec = importlib.util.spec_from_file_location("nova_act_entrypoint", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("NOVA_ACT_API_KEY", "test-key")
    monkeypatch.setenv("S3_ARTIFACT_BUCKET", "bucket")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    return module


class TestRegionFailFast:
    def test_non_us_east_1_exits_before_reading_payload(self, entrypoint, monkeypatch, capsys):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        monkeypatch.setenv("TASK_PAYLOAD", "not json")
        assert entrypoint.main() == 1
        assert "us-east-1" in capsys.readouterr().err


class TestPayloadRouting:
    def test_checkout_flow_payload_reaches_run_checkout_flow(self, entrypoint, monkeypatch, capsys):
        payload = {
            "task_id": "flow-1",
            "task_type": "checkout_flow",
            "starting_url": "https://example.com",
            "allowed_domains": ["example.com"],
            "steps": [{"step_name": "cart", "prompt": "Open the cart"}],
        }
        monkeypatch.setenv("TASK_PAYLOAD", json.dumps(payload))
        fake = CheckoutResult(
            flow_id="flow-1", status=TaskStatus.SUCCESS, completed_steps=1, total_steps=1, step_results=[]
        )
        with patch.object(entrypoint, "ObservabilityManager"), patch.object(
            entrypoint.WorkflowAgent, "run_checkout_flow", return_value=fake
        ) as run:
            assert entrypoint.main() == 0
        flow = run.call_args[0][0]
        assert flow.flow_id == "flow-1"
        assert flow.steps[0].step_index == 0
        assert json.loads(capsys.readouterr().out)["flow_id"] == "flow-1"

    def test_qa_test_payload_reaches_run_qa_test(self, entrypoint, monkeypatch):
        payload = {
            "task_id": "qa-1",
            "task_type": "qa_test",
            "starting_url": "https://example.com",
            "allowed_domains": ["example.com"],
            "steps": [{"action_prompt": "Read the title", "expected_outcome": "Example"}],
        }
        monkeypatch.setenv("TASK_PAYLOAD", json.dumps(payload))
        fake = QATestReport(
            test_id="qa-1", test_name="qa-1", status=TaskStatus.SUCCESS, total_steps=1,
            passed_steps=1, failed_steps=0, duration_seconds=0.0, step_reports=[],
        )
        with patch.object(entrypoint, "ObservabilityManager"), patch.object(
            entrypoint.WorkflowAgent, "run_qa_test", return_value=fake
        ) as run:
            assert entrypoint.main() == 0
        assert run.call_args[0][0].steps[0].expected_outcome == "Example"

    def test_batch_rejects_multi_step_tasks(self, entrypoint, monkeypatch):
        payload = {"tasks": [{"task_id": "a", "task_type": "qa_test", "starting_url": "https://example.com"}]}
        monkeypatch.setenv("TASK_PAYLOAD", json.dumps(payload))
        assert entrypoint.main() == 1

    def test_capture_toggle_and_log_group_ownership(self, entrypoint, monkeypatch):
        monkeypatch.setenv("CAPTURE_SCREENSHOTS", "false")
        with patch.object(entrypoint, "ObservabilityManager") as obs_cls:
            entrypoint._make_observability("nova-act-fleet", "bucket")
        cfg = obs_cls.call_args[0][0]
        assert cfg.capture_screenshots is False
        assert cfg.create_log_group is False
        assert cfg.cloudwatch_log_group == "/ecs/nova-act-fleet-workflow-agent"


class TestConcurrencyDefault:
    def test_model_and_entrypoint_defaults_agree(self, entrypoint):
        source = ENTRYPOINT.read_text(encoding="utf-8")
        default = FleetConfig.model_fields["concurrency_limit"].default
        assert f'payload.get("concurrency_limit", {default})' in source


class TestObservabilityLogGroup:
    def _manager(self, create: bool):
        with patch("nova_act_fleet.components.observability_manager.boto3") as boto3:
            logs = MagicMock()
            logs.exceptions.ResourceAlreadyExistsException = type("Exists", (Exception,), {})
            boto3.client.side_effect = lambda name, **_: logs if name == "logs" else MagicMock()
            mgr = ObservabilityManager(
                ObservabilityConfig(cloudwatch_log_group="/ecs/x", s3_bucket="b", create_log_group=create)
            )
        return mgr, logs

    def test_skips_create_log_group_when_iac_owns_it(self):
        mgr, logs = self._manager(create=False)
        mgr._ensure_log_stream()
        logs.create_log_group.assert_not_called()
        logs.create_log_stream.assert_called_once()

    def test_access_denied_on_create_falls_through_to_stream(self):
        mgr, logs = self._manager(create=True)
        logs.create_log_group.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "CreateLogGroup"
        )
        mgr._ensure_log_stream()
        logs.create_log_stream.assert_called_once()
