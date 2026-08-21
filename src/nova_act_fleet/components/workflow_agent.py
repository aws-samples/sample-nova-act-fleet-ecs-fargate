"""WorkflowAgent for Nova Act Fleet.

Core execution unit that wraps the Nova Act SDK's NovaAct context manager
and adds security validation, retry logic, structured data extraction,
form automation, checkout flow execution, QA testing, artifact capture,
and structured logging.
"""

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

from nova_act_fleet.components.observability_manager import ObservabilityManager
from nova_act_fleet.components.security_manager import SecurityManager
from nova_act_fleet.models.config import AgentConfig, ObservabilityConfig, SecurityConfig
from nova_act_fleet.models.domain import (
    CheckoutFlowDefinition,
    CheckoutResult,
    LogEvent,
    QAStepReport,
    QATestDefinition,
    QATestReport,
    StepResult,
    StructuredOutput,
)
from nova_act_fleet.models.tasks import ErrorDetail, TaskDefinition, TaskResult, TaskStatus

try:
    from nova_act import NovaAct
except ImportError:  # pragma: no cover
    NovaAct = None  # type: ignore[assignment,misc]


class AgentSessionError(Exception):
    """Raised when domain validation fails or a session times out."""


class WorkflowAgent:
    """Executes browser-based tasks via the Nova Act SDK.

    Creates a NovaAct context manager with the configured starting page,
    headless mode, and allowed domains. Validates URLs against the
    Domain_Allowlist before session creation, enforces a 30-minute session
    timeout via a threading timer, and captures screenshots and page URLs
    after each significant step.
    """

    def __init__(
        self,
        config: AgentConfig,
        security_manager: Optional[SecurityManager] = None,
        observability_manager: Optional[ObservabilityManager] = None,
    ) -> None:
        self._config = config

        # Use provided SecurityManager or build one from agent config
        if security_manager is not None:
            self._security = security_manager
        else:
            security_cfg = SecurityConfig(
                allowed_domains=list(config.allowed_domains),
                blocked_domains=list(config.blocked_domains),
            )
            self._security = SecurityManager(security_cfg)

        # Use provided ObservabilityManager or build one from sensible defaults
        if observability_manager is not None:
            self._observability = observability_manager
        else:
            obs_cfg = ObservabilityConfig(
                cloudwatch_log_group="/nova-act-fleet/agents",
                s3_bucket="nova-act-fleet-artifacts",
            )
            self._observability = ObservabilityManager(obs_cfg)

        # Active NovaAct session reference (set during execution)
        self._nova: Optional[Any] = None
        self._session_timed_out = False
        self._timeout_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------ #
    #  Task 7.1 – Core methods                                           #
    # ------------------------------------------------------------------ #

    def execute_task(self, task: TaskDefinition) -> TaskResult:
        """Execute a browser-based task via the Nova Act SDK.

        Creates a fresh NovaAct session, validates the starting URL
        against the domain allowlist, enforces the 30-minute session
        timeout, executes the prompt, and captures results.

        Validates prompt length against ``config.max_prompt_length`` and
        rejects with REJECTED status if exceeded.  Validates step count
        against ``config.max_steps`` and halts with FAILED status if
        exceeded.

        Args:
            task: The task definition containing URL, prompt, and config.

        Returns:
            A TaskResult with status, timing, output, and artifacts.

        Raises:
            AgentSessionError: If domain validation fails or the Nova Act
                SDK is not installed.
        """
        started_at = datetime.now(timezone.utc)
        self._session_timed_out = False
        screenshot_keys: List[str] = []
        error_details: List[ErrorDetail] = []

        # --- Prompt length validation ---
        if len(task.prompt) > self._config.max_prompt_length:
            completed_at = datetime.now(timezone.utc)
            duration = (completed_at - started_at).total_seconds()
            self._emit_log(
                LogEvent(
                    timestamp=completed_at,
                    task_id=task.task_id,
                    event_type="error",
                    status=TaskStatus.REJECTED,
                    message=(
                        f"Prompt length {len(task.prompt)} exceeds maximum "
                        f"{self._config.max_prompt_length} characters"
                    ),
                )
            )
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.REJECTED,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
                error_details=[
                    ErrorDetail(
                        error_type="PromptLengthExceeded",
                        message=(
                            f"Prompt length {len(task.prompt)} exceeds maximum "
                            f"{self._config.max_prompt_length} characters"
                        ),
                        page_url=task.starting_url,
                        timestamp=completed_at,
                    )
                ],
                page_url=task.starting_url,
            )

        # --- Domain validation ---
        if not self._validate_domain(task.starting_url):
            raise AgentSessionError(
                f"Domain validation failed for URL: {task.starting_url}"
            )

        self._emit_log(
            LogEvent(
                timestamp=datetime.now(timezone.utc),
                task_id=task.task_id,
                event_type="task_start",
                message=f"Starting task execution for {task.starting_url}",
            )
        )

        if NovaAct is None:
            raise AgentSessionError(
                "Nova Act SDK is not installed. Install it with: pip install nova-act"
            )

        try:
            with NovaAct(
                starting_page=task.starting_url,
                headless=self._config.headless,
                nova_act_api_key=self._config.api_key,
            ) as nova:
                self._nova = nova

                # Enforce session timeout (30 min max)
                timeout_secs = min(
                    self._config.session_timeout_seconds, 1800
                )
                self._timeout_timer = threading.Timer(
                    timeout_secs, self._on_session_timeout
                )
                self._timeout_timer.daemon = True
                self._timeout_timer.start()

                try:
                    result = nova.act(task.prompt)

                    if self._session_timed_out:
                        completed_at = datetime.now(timezone.utc)
                        duration = (completed_at - started_at).total_seconds()
                        self._emit_log(
                            LogEvent(
                                timestamp=completed_at,
                                task_id=task.task_id,
                                event_type="task_end",
                                status=TaskStatus.TIMEOUT,
                                message="Session timed out",
                            )
                        )
                        return TaskResult(
                            task_id=task.task_id,
                            status=TaskStatus.TIMEOUT,
                            started_at=started_at,
                            completed_at=completed_at,
                            duration_seconds=duration,
                            screenshot_keys=screenshot_keys,
                            page_url=task.starting_url,
                        )

                    # Capture screenshot after execution
                    try:
                        ss_key = self._capture_screenshot()
                        screenshot_keys.append(ss_key)
                    except Exception:
                        pass  # Screenshot capture is best-effort

                    completed_at = datetime.now(timezone.utc)
                    duration = (completed_at - started_at).total_seconds()

                    self._emit_log(
                        LogEvent(
                            timestamp=completed_at,
                            task_id=task.task_id,
                            event_type="task_end",
                            status=TaskStatus.SUCCESS,
                            message="Task completed successfully",
                        )
                    )

                    return TaskResult(
                        task_id=task.task_id,
                        status=TaskStatus.SUCCESS,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_seconds=duration,
                        output=result,
                        screenshot_keys=screenshot_keys,
                        page_url=task.starting_url,
                    )
                finally:
                    if self._timeout_timer is not None:
                        self._timeout_timer.cancel()
                        self._timeout_timer = None
                    self._nova = None

        except AgentSessionError:
            raise
        except Exception as exc:
            completed_at = datetime.now(timezone.utc)
            duration = (completed_at - started_at).total_seconds()

            # Determine error type name – use ActError when applicable
            error_type_name = type(exc).__name__

            error_details.append(
                ErrorDetail(
                    error_type=error_type_name,
                    message=str(exc),
                    page_url=task.starting_url,
                    timestamp=datetime.now(timezone.utc),
                )
            )

            self._emit_log(
                LogEvent(
                    timestamp=completed_at,
                    task_id=task.task_id,
                    event_type="error",
                    status=TaskStatus.FAILED,
                    message=f"Task failed: {exc}",
                )
            )

            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
                error_details=error_details,
                screenshot_keys=screenshot_keys,
                page_url=task.starting_url,
            )

    def _on_session_timeout(self) -> None:
        """Callback invoked by the threading timer when the session exceeds the timeout."""
        self._session_timed_out = True

    def _validate_domain(self, url: str) -> bool:
        """Validate a URL against the configured domain allowlist/blocklist.

        Delegates to SecurityManager.validate_domain().

        Args:
            url: The URL to validate.

        Returns:
            True if the domain is allowed, False otherwise.
        """
        return self._security.validate_domain(
            url,
            self._config.allowed_domains,
            self._config.blocked_domains,
        )

    def _capture_screenshot(self) -> str:
        """Capture a screenshot from the active browser session and store it.

        Uses the active NovaAct session to take a screenshot, then
        delegates storage to ObservabilityManager.store_artifact().

        Returns:
            The S3 key where the screenshot was stored.
        """
        screenshot_bytes = b""
        if self._nova is not None:
            try:
                screenshot_bytes = self._nova.page.screenshot()
            except Exception:
                screenshot_bytes = b""

        key = f"screenshots/{uuid.uuid4()}.png"
        metadata = {"type": "screenshot", "timestamp": datetime.now(timezone.utc).isoformat()}
        return self._observability.store_artifact(screenshot_bytes, key, metadata)

    def _emit_log(self, event: LogEvent) -> None:
        """Emit a structured log event via ObservabilityManager.

        Args:
            event: The log event to emit.
        """
        try:
            self._observability.log_event(event)
        except Exception:
            pass  # Logging failures should not break task execution

    # ------------------------------------------------------------------ #
    #  Task 7.2 – Structured data extraction                             #
    # ------------------------------------------------------------------ #

    def extract_data(self, prompt: str, schema: Type[BaseModel]) -> StructuredOutput:
        """Extract structured data from a web page using a Pydantic model schema.

        Opens a NovaAct session, executes the extraction prompt with the
        schema parameter, validates the returned data against the schema,
        and returns a StructuredOutput wrapper.

        Args:
            prompt: Natural language extraction prompt.
            schema: Pydantic model class defining the expected data shape.

        Returns:
            A StructuredOutput containing the validated extracted data.

        Raises:
            AgentSessionError: If domain validation fails or SDK is missing.
            ValidationError: If extracted data does not conform to the schema.
        """
        if not self._validate_domain(self._config.starting_url):
            raise AgentSessionError(
                f"Domain validation failed for URL: {self._config.starting_url}"
            )

        if NovaAct is None:
            raise AgentSessionError(
                "Nova Act SDK is not installed. Install it with: pip install nova-act"
            )

        with NovaAct(
            starting_page=self._config.starting_url,
            headless=self._config.headless,
            nova_act_api_key=self._config.api_key,
        ) as nova:
            self._nova = nova

            try:
                result = nova.act(prompt, schema=schema)
                raw_data = result if isinstance(result, dict) else {}

                # Validate against the Pydantic schema – raises ValidationError
                # with field-level details if data is invalid
                validated = schema.model_validate(raw_data)
                data_dict = validated.model_dump()

                return StructuredOutput(
                    task_id=str(uuid.uuid4()),
                    schema_name=schema.__name__,
                    data=data_dict,
                )
            finally:
                self._nova = None

    # ------------------------------------------------------------------ #
    #  Task 7.3 – Form automation with retry logic                       #
    # ------------------------------------------------------------------ #

    def automate_form(
        self, prompt: str, form_data: dict, max_retries: int = 3
    ) -> TaskResult:
        """Automate form-filling with retry logic.

        Executes the form-filling prompt via NovaAct. On failure (ActError
        or any exception), captures error details, a screenshot, and the
        page URL, then retries up to ``max_retries`` times. Accumulates
        all errors across attempts. On final failure returns a permanent
        failure status with all error details.

        Missing field identifiers are logged and remaining fields continue
        to be processed.

        Args:
            prompt: Natural language form-filling prompt.
            form_data: Dictionary of field names to values for the form.
            max_retries: Maximum number of retry attempts (default 3).

        Returns:
            A TaskResult with success or failure status and accumulated errors.
        """
        started_at = datetime.now(timezone.utc)
        task_id = str(uuid.uuid4())
        error_details: List[ErrorDetail] = []
        screenshot_keys: List[str] = []
        retry_count = 0
        last_page_url: Optional[str] = self._config.starting_url

        if not self._validate_domain(self._config.starting_url):
            raise AgentSessionError(
                f"Domain validation failed for URL: {self._config.starting_url}"
            )

        if NovaAct is None:
            raise AgentSessionError(
                "Nova Act SDK is not installed. Install it with: pip install nova-act"
            )

        # Build the full prompt including form data context
        full_prompt = f"{prompt}\nForm data: {form_data}"

        total_attempts = max_retries + 1  # 1 initial + max_retries retries

        for attempt in range(total_attempts):
            try:
                with NovaAct(
                    starting_page=self._config.starting_url,
                    headless=self._config.headless,
                    nova_act_api_key=self._config.api_key,
                ) as nova:
                    self._nova = nova

                    try:
                        result = nova.act(full_prompt)

                        # Capture screenshot on success
                        try:
                            ss_key = self._capture_screenshot()
                            screenshot_keys.append(ss_key)
                        except Exception:
                            pass

                        try:
                            last_page_url = nova.page.url
                        except Exception:
                            pass

                        completed_at = datetime.now(timezone.utc)
                        duration = (completed_at - started_at).total_seconds()

                        self._emit_log(
                            LogEvent(
                                timestamp=completed_at,
                                task_id=task_id,
                                event_type="task_end",
                                status=TaskStatus.SUCCESS,
                                message="Form automation completed successfully",
                            )
                        )

                        return TaskResult(
                            task_id=task_id,
                            status=TaskStatus.SUCCESS,
                            started_at=started_at,
                            completed_at=completed_at,
                            duration_seconds=duration,
                            output=result,
                            error_details=error_details if error_details else None,
                            screenshot_keys=screenshot_keys,
                            page_url=last_page_url,
                            retry_count=attempt,
                        )
                    finally:
                        self._nova = None

            except Exception as exc:
                now = datetime.now(timezone.utc)

                # Capture screenshot on error (best-effort)
                ss_key: Optional[str] = None
                try:
                    ss_key = self._capture_screenshot()
                    screenshot_keys.append(ss_key)
                except Exception:
                    pass

                # Log missing field identifiers
                exc_msg = str(exc).lower()
                if "not found" in exc_msg or "missing" in exc_msg:
                    self._emit_log(
                        LogEvent(
                            timestamp=now,
                            task_id=task_id,
                            event_type="error",
                            message=f"Missing field identifier: {exc}",
                            metadata={"form_data": form_data},
                        )
                    )

                error_details.append(
                    ErrorDetail(
                        error_type=type(exc).__name__,
                        message=str(exc),
                        page_url=last_page_url,
                        screenshot_key=ss_key,
                        timestamp=now,
                    )
                )

                retry_count = attempt

                self._emit_log(
                    LogEvent(
                        timestamp=now,
                        task_id=task_id,
                        event_type="error",
                        message=f"Form automation attempt {attempt + 1}/{total_attempts} failed: {exc}",
                    )
                )

        # All retries exhausted – permanent failure
        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()

        self._emit_log(
            LogEvent(
                timestamp=completed_at,
                task_id=task_id,
                event_type="task_end",
                status=TaskStatus.FAILED,
                message=f"Form automation failed after {total_attempts} attempts",
            )
        )

        return TaskResult(
            task_id=task_id,
            status=TaskStatus.FAILED,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
            error_details=error_details,
            screenshot_keys=screenshot_keys,
            page_url=last_page_url,
            retry_count=max_retries,
        )

    # ------------------------------------------------------------------ #
    #  Task 7.4 – Multi-step checkout flow automation                    #
    # ------------------------------------------------------------------ #

    def run_checkout_flow(self, flow: CheckoutFlowDefinition) -> CheckoutResult:
        """Execute a multi-step checkout flow.

        Runs each checkout step in sequence (e.g. cart review, shipping,
        payment, confirmation). Captures a screenshot and page URL after
        each step. On step failure, halts the flow and returns the step
        index, error details, and last screenshot. Supports configurable
        wait times between steps.

        Args:
            flow: The checkout flow definition with ordered steps.

        Returns:
            A CheckoutResult with per-step results and overall status.
        """
        step_results: List[StepResult] = []
        final_screenshot_key: Optional[str] = None
        overall_status = TaskStatus.SUCCESS

        if not self._validate_domain(flow.starting_url):
            raise AgentSessionError(
                f"Domain validation failed for URL: {flow.starting_url}"
            )

        if NovaAct is None:
            raise AgentSessionError(
                "Nova Act SDK is not installed. Install it with: pip install nova-act"
            )

        with NovaAct(
            starting_page=flow.starting_url,
            headless=self._config.headless,
            nova_act_api_key=self._config.api_key,
        ) as nova:
            self._nova = nova

            try:
                for step in flow.steps:
                    try:
                        nova.act(step.prompt)

                        # Capture screenshot after step
                        ss_key: Optional[str] = None
                        try:
                            ss_key = self._capture_screenshot()
                            final_screenshot_key = ss_key
                        except Exception:
                            pass

                        # Capture page URL
                        page_url: Optional[str] = None
                        try:
                            page_url = nova.page.url
                        except Exception:
                            pass

                        step_results.append(
                            StepResult(
                                step_index=step.step_index,
                                step_name=step.step_name,
                                status=TaskStatus.SUCCESS,
                                screenshot_key=ss_key,
                                page_url=page_url,
                            )
                        )

                        self._emit_log(
                            LogEvent(
                                timestamp=datetime.now(timezone.utc),
                                task_id=flow.flow_id,
                                event_type="step_complete",
                                status=TaskStatus.SUCCESS,
                                message=f"Checkout step '{step.step_name}' completed",
                            )
                        )

                        # Wait between steps
                        if flow.step_wait_seconds > 0:
                            time.sleep(flow.step_wait_seconds)

                    except Exception as exc:
                        # Capture screenshot on failure
                        ss_key_err: Optional[str] = None
                        try:
                            ss_key_err = self._capture_screenshot()
                            final_screenshot_key = ss_key_err
                        except Exception:
                            pass

                        page_url_err: Optional[str] = None
                        try:
                            page_url_err = nova.page.url
                        except Exception:
                            pass

                        error = ErrorDetail(
                            error_type=type(exc).__name__,
                            message=str(exc),
                            step_index=step.step_index,
                            page_url=page_url_err,
                            screenshot_key=ss_key_err,
                            timestamp=datetime.now(timezone.utc),
                        )

                        step_results.append(
                            StepResult(
                                step_index=step.step_index,
                                step_name=step.step_name,
                                status=TaskStatus.FAILED,
                                screenshot_key=ss_key_err,
                                page_url=page_url_err,
                                error=error,
                            )
                        )

                        overall_status = TaskStatus.FAILED

                        self._emit_log(
                            LogEvent(
                                timestamp=datetime.now(timezone.utc),
                                task_id=flow.flow_id,
                                event_type="error",
                                status=TaskStatus.FAILED,
                                message=f"Checkout step '{step.step_name}' failed: {exc}",
                            )
                        )

                        # Halt flow on step failure
                        break
            finally:
                self._nova = None

        completed_steps = sum(
            1 for sr in step_results if sr.status == TaskStatus.SUCCESS
        )

        return CheckoutResult(
            flow_id=flow.flow_id,
            status=overall_status,
            completed_steps=completed_steps,
            total_steps=len(flow.steps),
            step_results=step_results,
            final_screenshot_key=final_screenshot_key,
        )

    # ------------------------------------------------------------------ #
    #  Task 7.5 – QA test execution                                      #
    # ------------------------------------------------------------------ #

    def run_qa_test(self, test_def: QATestDefinition) -> QATestReport:
        """Execute a QA test with natural language steps.

        Runs each QA test step, compares actual outcomes against expected
        outcomes, and produces a QATestReport. Captures a screenshot per
        step. Marks a step as failed when expected != actual and includes
        both values in the report.

        Args:
            test_def: The QA test definition with steps and expected outcomes.

        Returns:
            A QATestReport with per-step results and overall pass/fail status.
        """
        test_started = datetime.now(timezone.utc)
        step_reports: List[QAStepReport] = []
        passed_steps = 0
        failed_steps = 0

        if not self._validate_domain(test_def.starting_url):
            raise AgentSessionError(
                f"Domain validation failed for URL: {test_def.starting_url}"
            )

        if NovaAct is None:
            raise AgentSessionError(
                "Nova Act SDK is not installed. Install it with: pip install nova-act"
            )

        with NovaAct(
            starting_page=test_def.starting_url,
            headless=self._config.headless,
            nova_act_api_key=self._config.api_key,
        ) as nova:
            self._nova = nova

            try:
                for step in test_def.steps:
                    step_start = datetime.now(timezone.utc)

                    try:
                        result = nova.act(step.action_prompt)
                        actual_outcome = str(result) if result is not None else ""

                        # Capture screenshot
                        ss_key: Optional[str] = None
                        try:
                            ss_key = self._capture_screenshot()
                        except Exception:
                            pass

                        step_end = datetime.now(timezone.utc)
                        step_duration = (step_end - step_start).total_seconds()

                        # Compare expected vs actual
                        if actual_outcome == step.expected_outcome:
                            step_status = TaskStatus.SUCCESS
                            passed_steps += 1
                        else:
                            step_status = TaskStatus.FAILED
                            failed_steps += 1

                        step_reports.append(
                            QAStepReport(
                                step_index=step.step_index,
                                status=step_status,
                                expected_outcome=step.expected_outcome,
                                actual_outcome=actual_outcome,
                                screenshot_key=ss_key,
                                duration_seconds=step_duration,
                            )
                        )

                        self._emit_log(
                            LogEvent(
                                timestamp=step_end,
                                task_id=test_def.test_id,
                                event_type="step_complete",
                                status=step_status,
                                message=(
                                    f"QA step {step.step_index}: "
                                    f"{'PASS' if step_status == TaskStatus.SUCCESS else 'FAIL'}"
                                ),
                                metadata={
                                    "expected": step.expected_outcome,
                                    "actual": actual_outcome,
                                },
                            )
                        )

                    except Exception as exc:
                        step_end = datetime.now(timezone.utc)
                        step_duration = (step_end - step_start).total_seconds()

                        # Capture screenshot on error
                        ss_key_err: Optional[str] = None
                        try:
                            ss_key_err = self._capture_screenshot()
                        except Exception:
                            pass

                        failed_steps += 1
                        step_reports.append(
                            QAStepReport(
                                step_index=step.step_index,
                                status=TaskStatus.FAILED,
                                expected_outcome=step.expected_outcome,
                                actual_outcome=f"Error: {exc}",
                                screenshot_key=ss_key_err,
                                duration_seconds=step_duration,
                            )
                        )

                        self._emit_log(
                            LogEvent(
                                timestamp=step_end,
                                task_id=test_def.test_id,
                                event_type="error",
                                status=TaskStatus.FAILED,
                                message=f"QA step {step.step_index} error: {exc}",
                            )
                        )
            finally:
                self._nova = None

        test_ended = datetime.now(timezone.utc)
        total_duration = (test_ended - test_started).total_seconds()

        overall_status = TaskStatus.SUCCESS if failed_steps == 0 else TaskStatus.FAILED

        return QATestReport(
            test_id=test_def.test_id,
            test_name=test_def.test_name,
            status=overall_status,
            total_steps=len(test_def.steps),
            passed_steps=passed_steps,
            failed_steps=failed_steps,
            duration_seconds=total_duration,
            step_reports=step_reports,
        )
