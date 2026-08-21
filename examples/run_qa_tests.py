#!/usr/bin/env python3
"""Example: Run QA tests using natural language test definitions.

Demonstrates how to define and execute browser-based QA tests with
the WorkflowAgent, comparing expected vs actual outcomes.

Prerequisites:
    - pip install nova-act nova-act-fleet
    - Set NOVA_ACT_API_KEY in a .env file at the project root,
      or export it in your shell.
    - AWS credentials configured for CloudWatch/S3 access
"""

import os

from dotenv import load_dotenv

from nova_act_fleet.agent import (
    AgentConfig,
    QATestDefinition,
    QATestStep,
    WorkflowAgent,
)

load_dotenv()


def main() -> None:
    api_key = os.environ.get("NOVA_ACT_API_KEY")
    if not api_key:
        raise SystemExit(
            "NOVA_ACT_API_KEY is not set. Add it to a .env file at the project "
            "root or export it in your shell."
        )

    config = AgentConfig(
        starting_url="https://app.example.com",
        api_key=api_key,
        allowed_domains=["app.example.com"],
        headless=True,
    )

    agent = WorkflowAgent(config=config)

    # Define a QA test
    test_def = QATestDefinition(
        test_id="qa-login-001",
        test_name="Login Flow Validation",
        starting_url="https://app.example.com/login",
        steps=[
            QATestStep(
                step_index=0,
                action_prompt="Verify the login page displays a username and password field.",
                expected_outcome="Login form is visible with username and password fields.",
            ),
            QATestStep(
                step_index=1,
                action_prompt="Enter username 'testuser' and password 'testpass123' and click Login.",
                expected_outcome="User is redirected to the dashboard page.",
            ),
            QATestStep(
                step_index=2,
                action_prompt="Verify the dashboard shows a welcome message for 'testuser'.",
                expected_outcome="Welcome message displays 'Hello, testuser'.",
            ),
        ],
    )

    # Execute the QA test
    report = agent.run_qa_test(test_def)

    print(f"Test:           {report.test_name} ({report.test_id})")
    print(f"Status:         {report.status.value}")
    print(f"Passed/Failed:  {report.passed_steps}/{report.failed_steps}")
    print(f"Duration:       {report.duration_seconds:.2f}s")

    for sr in report.step_reports:
        status_icon = "✓" if sr.status.value == "success" else "✗"
        print(f"  {status_icon} Step {sr.step_index}: {sr.status.value} ({sr.duration_seconds:.2f}s)")
        if sr.status.value == "failed":
            print(f"    Expected: {sr.expected_outcome}")
            print(f"    Actual:   {sr.actual_outcome}")


if __name__ == "__main__":
    main()
