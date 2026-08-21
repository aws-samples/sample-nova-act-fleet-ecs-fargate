#!/usr/bin/env python3
"""Example: Execute a single task with a WorkflowAgent.

Demonstrates how to configure and run a single browser automation task
using the Nova Act Fleet WorkflowAgent.

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
    TaskDefinition,
    WorkflowAgent,
)

# Load NOVA_ACT_API_KEY (and any other config) from a local .env file.
load_dotenv()


def main() -> None:
    api_key = os.environ.get("NOVA_ACT_API_KEY")
    if not api_key:
        raise SystemExit(
            "NOVA_ACT_API_KEY is not set. Add it to a .env file at the project "
            "root or export it in your shell. Get a key at "
            "https://nova.amazon.com/act?tab=dev_tools"
        )

    # Configure the agent
    config = AgentConfig(
        starting_url="https://nova.amazon.com/act",
        api_key=api_key,
        allowed_domains=["nova.amazon.com", "amazon.com"],
        headless=True,
        session_timeout_seconds=1800,
        max_steps=100,
        max_prompt_length=10000,
    )

    agent = WorkflowAgent(config=config)

    # Define a task
    task = TaskDefinition(
        task_id="task-001",
        task_type="custom",
        starting_url="https://nova.amazon.com/act",
        prompt="Start a New session and ask a question about how agents are built",
    )

    # Execute the task
    result = agent.execute_task(task)

    print(f"Task ID:   {result.task_id}")
    print(f"Status:    {result.status.value}")
    print(f"Duration:  {result.duration_seconds:.2f}s")
    if result.output:
        print(f"Output:    {result.output}")
    if result.error_details:
        for err in result.error_details:
            print(f"Error:     [{err.error_type}] {err.message}")


if __name__ == "__main__":
    main()
