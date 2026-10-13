"""WorkflowAgent for Nova Act Fleet.

Core execution unit that wraps the Nova Act SDK's NovaAct context manager
and adds security validation, retry logic, structured data extraction,
form automation, checkout flow execution, QA testing, artifact capture,
and structured logging.

Every public method opens its browser session through a single factory
(``_session``) so that the same controls apply on every path:

* Pre-flight: prompt length and prompt filtering (layer 1) and starting-URL
  domain validation (layer 2) run before a session opens.
* In-session: the Nova Act SDK ``state_guardrail`` re-checks every observed
  URL against the domain allowlist (layer 2), and ``SecurityOptions``
  blocks ``file://`` access and uploads unless explicitly allowlisted
  (layer 4). No custom tools are registered with the SDK (layer 3).
* Artifacts: screenshots are stored under ``screenshots/<task_id>/`` and the
  SDK's per-step session trace under ``traces/<task_id>/`` when
  ``ObservabilityConfig.capture_screenshots`` is True.
"""

import logging
import mimetypes
import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, List, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

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

try:
    from nova_act import ActStateGuardrailError, GuardrailDecision, SecurityOptions
except ImportError:  # pragma: no cover

    class ActStateGuardrailError(Exception):  # type: ignore[no-redef]
        """Fallback when the Nova Act SDK is not installed."""

    GuardrailDecision = None  # type: ignore[assignment,misc]
    SecurityOptions = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# URLs the browser can legitimately report before or between page loads.
_NEUTRAL_URLS = frozenset({"", "about:blank"})


class AgentSessionError(Exception):
    """Raised when domain validation fails or a session cannot be opened."""


class PromptRejectedError(AgentSessionError):
    """Raised when a prompt fails pre-flight length or filter checks."""


class _SessionState:
    """Per-thread, per-session mutable state.

    ``FleetOrchestrator`` shares one WorkflowAgent across a thread pool, so
    session state must not live on the agent instance itself.
    """

    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id
        self.nova: Optional[Any] = None
        self.timed_out = False
        self.blocked_urls: List[str] = []
        self.trace_keys: List[str] = []
        self._screenshot_seq = 0

    def mark_timed_out(self) -> None:
        self.timed_out = True

    def next_screenshot_index(self) -> int:
        self._screenshot_seq += 1
        return self._screenshot_seq


class WorkflowAgent:
    """Executes browser-based tasks via the Nova Act SDK.

    Validates prompts and the starting URL before a session opens, enforces
    the domain allowlist on every navigation during the session, enforces a
    30-minute session timeout via a threading timer, and captures screenshots
    and the SDK's per-step session trace as audit artifacts.
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

        self._local = threading.local()

    # ------------------------------------------------------------------ #
    #  Thread-local session state                                        #
    # ------------------------------------------------------------------ #

    def _state(self) -> _SessionState:
        state = getattr(self._local, "session", None)
        if state is None:
            state = _SessionState()
            self._local.session = state
        return state

    @property
    def _nova(self) -> Optional[Any]:
        return self._state().nova

    @_nova.setter
    def _nova(self, value: Optional[Any]) -> None:
        self._state().nova = value

    @property
    def _session_timed_out(self) -> bool:
        return self._state().timed_out

    @_session_timed_out.setter
    def _session_timed_out(self, value: bool) -> None:
        self._state().timed_out = value

    def _on_session_timeout(self) -> None:
        """Mark the current thread's session as timed out."""
        self._state().mark_timed_out()

    # ------------------------------------------------------------------ #
    #  Shared pre-flight and session factory                             #
    # ------------------------------------------------------------------ #

    def _check_prompt(self, prompt: str) -> Optional[Tuple[str, str]]:
        """Return ``(error_type, message)`` if the prompt must be rejected."""
        if len(prompt) > self._config.max_prompt_length:
            return (
                "PromptLengthExceeded",
                f"Prompt length {len(prompt)} exceeds maximum "
                f"{self._config.max_prompt_length} characters",
            )
        verdict = self._security.filter_prompt(prompt)
        if not verdict.allowed:
            return ("PromptFilterBlocked", verdict.blocked_reason or "Prompt blocked by input filter")
        return None

    def _preflight(self, starting_url: str, prompts: List[str]) -> None:
        """Validate prompts and the starting URL; raise if either fails."""
        for prompt in prompts:
            issue = self._check_prompt(prompt)
            if issue is not None:
                raise PromptRejectedError(f"{issue[0]}: {issue[1]}")
        if not self._validate_domain(starting_url):
            raise AgentSessionError(f"Domain validation failed for URL: {starting_url}")

    def _validate_domain(self, url: str) -> bool:
        """Validate a URL against the configured domain allowlist/blocklist.

        Delegates to SecurityManager.validate_domain().
        """
        return self._security.validate_domain(
            url,
            self._config.allowed_domains,
            self._config.blocked_domains,
        )

    def _make_domain_guardrail(self, state: _SessionState) -> Callable[[Any], Any]:
        """Build a Nova Act ``state_guardrail`` that enforces the allowlist.

        The SDK calls the guardrail on every observation during ``act()`` and
        on every programmatic navigation. A BLOCK decision makes ``act()``
        raise ``ActStateGuardrailError``.
        """

        def guardrail(input_state: Any) -> Any:
            url = getattr(input_state, "browser_url", "") or ""
            if url in _NEUTRAL_URLS or self._validate_domain(url):
                return GuardrailDecision.PASS
            state.blocked_urls.append(url)
            return GuardrailDecision.BLOCK

        return guardrail

    def _capture_enabled(self) -> bool:
        return bool(getattr(self._observability, "capture_screenshots", True))

    def _nova_kwargs(self, starting_url: str, state: _SessionState, trace_dir: Optional[str]) -> dict:
        kwargs: dict = {
            "starting_page": starting_url,
            "headless": self._config.headless,
            "nova_act_api_key": self._config.api_key,
        }
        if SecurityOptions is not None:
            file_paths = self._security.allowed_file_paths()
            kwargs["security_options"] = SecurityOptions(
                allowed_file_open_paths=file_paths,
                allowed_file_upload_paths=file_paths,
            )
        if self._config.enforce_domains_during_session and GuardrailDecision is not None:
            kwargs["state_guardrail"] = self._make_domain_guardrail(state)
        if trace_dir is not None:
            kwargs["logs_directory"] = trace_dir
        return kwargs

    @contextmanager
    def _session(self, starting_url: str, task_id: str) -> Iterator[_SessionState]:
        """Open a guarded NovaAct session and yield its state.

        Starts the session timeout timer, and on exit uploads the SDK's
        per-step session trace (when capture is enabled).
        """
        if NovaAct is None:
            raise AgentSessionError(
                "Nova Act SDK is not installed. Install it with: pip install nova-act"
            )

        state = _SessionState(task_id)
        self._local.session = state
        trace_dir = tempfile.mkdtemp(prefix="nova-act-trace-") if self._capture_enabled() else None

        timer = threading.Timer(
            min(self._config.session_timeout_seconds, 1800), state.mark_timed_out
        )
        timer.daemon = True
        try:
            with NovaAct(**self._nova_kwargs(starting_url, state, trace_dir)) as nova:
                state.nova = nova
                timer.start()
                try:
                    yield state
                finally:
                    state.nova = None
        finally:
            timer.cancel()
            if trace_dir is not None:
                state.trace_keys = self._upload_trace(task_id, trace_dir)

    def _upload_trace(self, task_id: str, trace_dir: str) -> List[str]:
        """Upload every file the SDK wrote to ``trace_dir`` and clean up.

        The local directory is removed only when every file uploaded, so a
        failed upload leaves the trace on disk for local inspection.
        """
        keys: List[str] = []
        failed = False
        for root, _dirs, files in os.walk(trace_dir):
            for name in sorted(files):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, trace_dir).replace(os.sep, "/")
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

                def _put(p: str = path, r: str = rel, ct: str = content_type) -> str:
                    with open(p, "rb") as fh:
                        return self._observability.store_artifact(
                            fh.read(),
                            f"traces/{task_id}/{r}",
                            {"type": "session_trace", "task_id": task_id},
                            content_type=ct,
                        )

                key = self._best_effort("session trace upload", _put)
                if key:
                    keys.append(key)
                else:
                    failed = True
        if failed:
            logger.warning("Session trace for task %s left at %s", task_id, trace_dir)
        else:
            shutil.rmtree(trace_dir, ignore_errors=True)
        return keys

    @staticmethod
    def _best_effort(what: str, fn: Callable[[], _T], default: Optional[_T] = None) -> Optional[_T]:
        """Run a metadata-capture call whose failure must not fail the task."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            logger.warning("Best-effort %s failed: %s", what, exc)
            return default

    def _blocked_url(self, exc: BaseException, state: _SessionState) -> Optional[str]:
        """Return the URL a guardrail blocked, if ``exc`` came from the guardrail."""
        if state.blocked_urls:
            return state.blocked_urls[-1]
        if isinstance(exc, ActStateGuardrailError):
            return "unknown"
        return None

    @staticmethod
    def _guardrail_message(url: str) -> str:
        return f"Navigation to {url} blocked: host is not in allowed_domains or is in blocked_domains"

    def _capture_screenshot(self, task_id: Optional[str] = None, label: str = "step") -> Optional[str]:
        """Capture a screenshot from the active session and store it in S3.

        Stored at ``screenshots/<task_id>/<NNN>-<label>.png``. Returns None
        when capture is disabled or the page produced no image.
        """
        if not self._capture_enabled():
            return None
        state = self._state()
        if state.nova is None:
            return None
        screenshot_bytes = self._best_effort("screenshot", lambda: state.nova.page.screenshot(), b"")
        if not screenshot_bytes:
            return None
        owner = task_id or state.task_id or "unassigned"
        key = f"screenshots/{owner}/{state.next_screenshot_index():03d}-{label}.png"
        metadata = {
            "type": "screenshot",
            "task_id": owner,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._observability.store_artifact(screenshot_bytes, key, metadata, content_type="image/png")

    def _safe_screenshot(self, task_id: str, label: str) -> Optional[str]:
        return self._best_effort("screenshot upload", lambda: self._capture_screenshot(task_id, label))

    def _page_url(self) -> Optional[str]:
        nova = self._state().nova
        if nova is None:
            return None
        url = self._best_effort("page URL read", lambda: nova.page.url)
        return url if isinstance(url, str) else None

    def _emit_log(self, event: LogEvent) -> None:
        """Emit a structured log event via ObservabilityManager.

        Logging failures must not break task execution; they are reported
        to the process logger (stderr, which the container ships to
        CloudWatch Logs) instead of being silently discarded.
        """
        self._best_effort("structured log emission", lambda: self._observability.log_event(event))

    # ------------------------------------------------------------------ #
    #  Core task execution                                               #
    # ------------------------------------------------------------------ #

    def execute_task(self, task: TaskDefinition) -> TaskResult:
        """Execute a browser-based task via the Nova Act SDK.

        Rejects the task (status REJECTED) if the prompt is too long or
        matches an input-filter pattern, or if the agent navigates to a host
        outside the allowlist during the session.

        Raises:
            AgentSessionError: If the starting URL fails domain validation or
                the Nova Act SDK is not installed.
        """
        started_at = datetime.now(timezone.utc)
        screenshot_keys: List[str] = []

        def _finish(status: TaskStatus, **fields: Any) -> TaskResult:
            completed_at = datetime.now(timezone.utc)
            return TaskResult(
                task_id=task.task_id,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - started_at).total_seconds(),
                screenshot_keys=screenshot_keys,
                page_url=task.starting_url,
                **fields,
            )

        def _reject(error_type: str, message: str) -> TaskResult:
            now = datetime.now(timezone.utc)
            self._emit_log(
                LogEvent(
                    timestamp=now,
                    task_id=task.task_id,
                    event_type="error",
                    status=TaskStatus.REJECTED,
                    message=message,
                )
            )
            return _finish(
                TaskStatus.REJECTED,
                error_details=[
                    ErrorDetail(
                        error_type=error_type,
                        message=message,
                        page_url=task.starting_url,
                        timestamp=now,
                    )
                ],
            )

        # --- Pre-flight: prompt length and input filter (layer 1) ---
        issue = self._check_prompt(task.prompt)
        if issue is not None:
            return _reject(*issue)

        # --- Pre-flight: starting-URL domain validation (layer 2) ---
        if not self._validate_domain(task.starting_url):
            raise AgentSessionError(f"Domain validation failed for URL: {task.starting_url}")

        self._emit_log(
            LogEvent(
                timestamp=datetime.now(timezone.utc),
                task_id=task.task_id,
                event_type="task_start",
                message=f"Starting task execution for {task.starting_url}",
            )
        )

        result: Optional[TaskResult] = None
        state: Optional[_SessionState] = None
        try:
            with self._session(task.starting_url, task.task_id) as state:
                try:
                    output = state.nova.act(task.prompt)
                except Exception:
                    shot = self._safe_screenshot(task.task_id, "error")
                    if shot:
                        screenshot_keys.append(shot)
                    raise

                if state.timed_out:
                    self._emit_log(
                        LogEvent(
                            timestamp=datetime.now(timezone.utc),
                            task_id=task.task_id,
                            event_type="task_end",
                            status=TaskStatus.TIMEOUT,
                            message="Session timed out",
                        )
                    )
                    result = _finish(TaskStatus.TIMEOUT)
                else:
                    shot = self._safe_screenshot(task.task_id, "final")
                    if shot:
                        screenshot_keys.append(shot)
                    self._emit_log(
                        LogEvent(
                            timestamp=datetime.now(timezone.utc),
                            task_id=task.task_id,
                            event_type="task_end",
                            status=TaskStatus.SUCCESS,
                            message="Task completed successfully",
                        )
                    )
                    result = _finish(TaskStatus.SUCCESS, output=output)

        except AgentSessionError:
            raise
        except Exception as exc:
            blocked = self._blocked_url(exc, state) if state is not None else None
            if blocked is not None:
                result = _reject("DomainGuardrailBlocked", self._guardrail_message(blocked))
            else:
                now = datetime.now(timezone.utc)
                self._emit_log(
                    LogEvent(
                        timestamp=now,
                        task_id=task.task_id,
                        event_type="error",
                        status=TaskStatus.FAILED,
                        message=f"Task failed: {exc}",
                    )
                )
                result = _finish(
                    TaskStatus.FAILED,
                    error_details=[
                        ErrorDetail(
                            error_type=type(exc).__name__,
                            message=str(exc),
                            page_url=task.starting_url,
                            timestamp=now,
                        )
                    ],
                )

        if state is not None:
            result.trace_keys = list(state.trace_keys)
        return result

    # ------------------------------------------------------------------ #
    #  Structured data extraction                                        #
    # ------------------------------------------------------------------ #

    def extract_data(self, prompt: str, schema: Type[BaseModel]) -> StructuredOutput:
        """Extract structured data from a web page using a Pydantic model schema.

        Raises:
            AgentSessionError: If pre-flight validation fails, the SDK is
                missing, or the domain guardrail blocks a navigation.
            ValidationError: If extracted data does not conform to the schema.
        """
        self._preflight(self._config.starting_url, [prompt])
        task_id = str(uuid.uuid4())

        with self._session(self._config.starting_url, task_id) as state:
            try:
                result = state.nova.act(prompt, schema=schema)
            except Exception as exc:
                blocked = self._blocked_url(exc, state)
                if blocked is not None:
                    raise AgentSessionError(self._guardrail_message(blocked)) from exc
                raise

        raw_data = result if isinstance(result, dict) else {}
        # Validate against the Pydantic schema; raises ValidationError with
        # field-level details if data is invalid.
        validated = schema.model_validate(raw_data)
        return StructuredOutput(
            task_id=task_id,
            schema_name=schema.__name__,
            data=validated.model_dump(),
        )

    # ------------------------------------------------------------------ #
    #  Form automation with retry logic                                  #
    # ------------------------------------------------------------------ #

    def automate_form(self, prompt: str, form_data: dict, max_retries: int = 3) -> TaskResult:
        """Automate form-filling with retry logic.

        Retries up to ``max_retries`` times on failure, capturing a
        screenshot and page URL per failed attempt. A domain-guardrail block
        is not retried; it returns REJECTED immediately.
        """
        started_at = datetime.now(timezone.utc)
        task_id = str(uuid.uuid4())
        error_details: List[ErrorDetail] = []
        screenshot_keys: List[str] = []
        trace_keys: List[str] = []
        last_page_url: Optional[str] = self._config.starting_url

        full_prompt = f"{prompt}\nForm data: {form_data}"
        self._preflight(self._config.starting_url, [full_prompt])

        def _finish(status: TaskStatus, **fields: Any) -> TaskResult:
            completed_at = datetime.now(timezone.utc)
            return TaskResult(
                task_id=task_id,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - started_at).total_seconds(),
                screenshot_keys=screenshot_keys,
                trace_keys=trace_keys,
                page_url=last_page_url,
                **fields,
            )

        total_attempts = max_retries + 1  # 1 initial + max_retries retries

        for attempt in range(total_attempts):
            state: Optional[_SessionState] = None
            try:
                with self._session(self._config.starting_url, task_id) as state:
                    try:
                        result = state.nova.act(full_prompt)
                    except Exception:
                        shot = self._safe_screenshot(task_id, f"attempt{attempt + 1}-error")
                        if shot:
                            screenshot_keys.append(shot)
                        last_page_url = self._page_url() or last_page_url
                        raise
                    shot = self._safe_screenshot(task_id, "final")
                    if shot:
                        screenshot_keys.append(shot)
                    last_page_url = self._page_url() or last_page_url
                trace_keys.extend(state.trace_keys)

                self._emit_log(
                    LogEvent(
                        timestamp=datetime.now(timezone.utc),
                        task_id=task_id,
                        event_type="task_end",
                        status=TaskStatus.SUCCESS,
                        message="Form automation completed successfully",
                    )
                )
                return _finish(
                    TaskStatus.SUCCESS,
                    output=result,
                    error_details=error_details or None,
                    retry_count=attempt,
                )

            except AgentSessionError:
                raise
            except Exception as exc:
                now = datetime.now(timezone.utc)
                if state is not None:
                    trace_keys.extend(state.trace_keys)

                blocked = self._blocked_url(exc, state) if state is not None else None
                if blocked is not None:
                    error_details.append(
                        ErrorDetail(
                            error_type="DomainGuardrailBlocked",
                            message=self._guardrail_message(blocked),
                            page_url=blocked,
                            timestamp=now,
                        )
                    )
                    self._emit_log(
                        LogEvent(
                            timestamp=now,
                            task_id=task_id,
                            event_type="error",
                            status=TaskStatus.REJECTED,
                            message=self._guardrail_message(blocked),
                        )
                    )
                    return _finish(TaskStatus.REJECTED, error_details=error_details, retry_count=attempt)

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
                        screenshot_key=screenshot_keys[-1] if screenshot_keys else None,
                        timestamp=now,
                    )
                )
                self._emit_log(
                    LogEvent(
                        timestamp=now,
                        task_id=task_id,
                        event_type="error",
                        message=f"Form automation attempt {attempt + 1}/{total_attempts} failed: {exc}",
                    )
                )

        # All retries exhausted: permanent failure
        self._emit_log(
            LogEvent(
                timestamp=datetime.now(timezone.utc),
                task_id=task_id,
                event_type="task_end",
                status=TaskStatus.FAILED,
                message=f"Form automation failed after {total_attempts} attempts",
            )
        )
        return _finish(TaskStatus.FAILED, error_details=error_details, retry_count=max_retries)

    # ------------------------------------------------------------------ #
    #  Multi-step checkout flow automation                               #
    # ------------------------------------------------------------------ #

    def run_checkout_flow(self, flow: CheckoutFlowDefinition) -> CheckoutResult:
        """Execute a multi-step checkout flow.

        Runs each step in sequence in one browser session, capturing a
        screenshot and page URL after each step. Halts on the first failed
        step, on a domain-guardrail block (status REJECTED), or when the
        session timeout elapses (status TIMEOUT).
        """
        step_results: List[StepResult] = []
        final_screenshot_key: Optional[str] = None
        overall_status = TaskStatus.SUCCESS

        self._preflight(flow.starting_url, [step.prompt for step in flow.steps])

        with self._session(flow.starting_url, flow.flow_id) as state:
            for step in flow.steps:
                if state.timed_out:
                    overall_status = TaskStatus.TIMEOUT
                    break
                label = f"step{step.step_index}-{step.step_name}"
                try:
                    state.nova.act(step.prompt)
                except Exception as exc:
                    ss_key = self._safe_screenshot(flow.flow_id, f"{label}-error")
                    final_screenshot_key = ss_key or final_screenshot_key
                    page_url = self._page_url()
                    blocked = self._blocked_url(exc, state)
                    error = ErrorDetail(
                        error_type="DomainGuardrailBlocked" if blocked else type(exc).__name__,
                        message=self._guardrail_message(blocked) if blocked else str(exc),
                        step_index=step.step_index,
                        page_url=blocked or page_url,
                        screenshot_key=ss_key,
                        timestamp=datetime.now(timezone.utc),
                    )
                    step_results.append(
                        StepResult(
                            step_index=step.step_index,
                            step_name=step.step_name,
                            status=TaskStatus.REJECTED if blocked else TaskStatus.FAILED,
                            screenshot_key=ss_key,
                            page_url=page_url,
                            error=error,
                        )
                    )
                    overall_status = TaskStatus.REJECTED if blocked else TaskStatus.FAILED
                    self._emit_log(
                        LogEvent(
                            timestamp=datetime.now(timezone.utc),
                            task_id=flow.flow_id,
                            event_type="error",
                            status=overall_status,
                            message=f"Checkout step '{step.step_name}' failed: {error.message}",
                        )
                    )
                    break  # Halt flow on step failure

                ss_key = self._safe_screenshot(flow.flow_id, label)
                final_screenshot_key = ss_key or final_screenshot_key
                step_results.append(
                    StepResult(
                        step_index=step.step_index,
                        step_name=step.step_name,
                        status=TaskStatus.SUCCESS,
                        screenshot_key=ss_key,
                        page_url=self._page_url(),
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
                if flow.step_wait_seconds > 0:
                    time.sleep(flow.step_wait_seconds)

        return CheckoutResult(
            flow_id=flow.flow_id,
            status=overall_status,
            completed_steps=sum(1 for sr in step_results if sr.status == TaskStatus.SUCCESS),
            total_steps=len(flow.steps),
            step_results=step_results,
            final_screenshot_key=final_screenshot_key,
            trace_keys=list(state.trace_keys),
        )

    # ------------------------------------------------------------------ #
    #  QA test execution                                                 #
    # ------------------------------------------------------------------ #

    def run_qa_test(self, test_def: QATestDefinition) -> QATestReport:
        """Execute a QA test with natural language steps.

        Runs each step, compares actual against expected outcomes, and
        captures a screenshot per step. A domain-guardrail block or session
        timeout stops the test.
        """
        test_started = datetime.now(timezone.utc)
        step_reports: List[QAStepReport] = []
        passed_steps = 0
        failed_steps = 0
        halted_status: Optional[TaskStatus] = None

        self._preflight(test_def.starting_url, [step.action_prompt for step in test_def.steps])

        with self._session(test_def.starting_url, test_def.test_id) as state:
            for step in test_def.steps:
                if state.timed_out:
                    halted_status = TaskStatus.TIMEOUT
                    break
                step_start = datetime.now(timezone.utc)
                label = f"step{step.step_index}"
                try:
                    result = state.nova.act(step.action_prompt)
                except Exception as exc:
                    step_end = datetime.now(timezone.utc)
                    blocked = self._blocked_url(exc, state)
                    message = self._guardrail_message(blocked) if blocked else str(exc)
                    failed_steps += 1
                    step_reports.append(
                        QAStepReport(
                            step_index=step.step_index,
                            status=TaskStatus.REJECTED if blocked else TaskStatus.FAILED,
                            expected_outcome=step.expected_outcome,
                            actual_outcome=f"Error: {message}",
                            screenshot_key=self._safe_screenshot(test_def.test_id, f"{label}-error"),
                            duration_seconds=(step_end - step_start).total_seconds(),
                        )
                    )
                    self._emit_log(
                        LogEvent(
                            timestamp=step_end,
                            task_id=test_def.test_id,
                            event_type="error",
                            status=TaskStatus.REJECTED if blocked else TaskStatus.FAILED,
                            message=f"QA step {step.step_index} error: {message}",
                        )
                    )
                    if blocked:
                        halted_status = TaskStatus.REJECTED
                        break
                    continue

                actual_outcome = str(result) if result is not None else ""
                ss_key = self._safe_screenshot(test_def.test_id, label)
                step_end = datetime.now(timezone.utc)
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
                        duration_seconds=(step_end - step_start).total_seconds(),
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
                        metadata={"expected": step.expected_outcome, "actual": actual_outcome},
                    )
                )

        if halted_status is not None:
            overall_status = halted_status
        else:
            overall_status = TaskStatus.SUCCESS if failed_steps == 0 else TaskStatus.FAILED

        return QATestReport(
            test_id=test_def.test_id,
            test_name=test_def.test_name,
            status=overall_status,
            total_steps=len(test_def.steps),
            passed_steps=passed_steps,
            failed_steps=failed_steps,
            duration_seconds=(datetime.now(timezone.utc) - test_started).total_seconds(),
            step_reports=step_reports,
            trace_keys=list(state.trace_keys),
        )
