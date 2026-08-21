#!/usr/bin/env python3
"""Example: Process a batch of tasks with the FleetOrchestrator.

Demonstrates parallel task execution, result aggregation, and HITL
escalation using the Nova Act Fleet FleetOrchestrator.

Prerequisites:
    - pip install nova-act nova-act-fleet
    - Set NOVA_ACT_API_KEY in a .env file at the project root,
      or export it in your shell.
    - AWS credentials configured for CloudWatch/S3/SNS access
"""

import os

from dotenv import load_dotenv

from nova_act_fleet.agent import (
    AgentConfig,
    DeploymentPattern,
    FleetConfig,
    FleetOrchestrator,
    TaskDefinition,
)

load_dotenv()


def main() -> None:
    api_key = os.environ.get("NOVA_ACT_API_KEY")
    if not api_key:
        raise SystemExit(
            "NOVA_ACT_API_KEY is not set. Add it to a .env file at the project "
            "root or export it in your shell."
        )

    # Configure the fleet. Wikipedia is used as the target because:
    #   - No CAPTCHAs or bot-detection walls (unlike major retail sites),
    #     which is what a batch demo needs to reliably pass.
    #   - Stable, low-JS article layout that doesn't A/B test.
    #   - Terms of use permit automated read access at reasonable rates.
    # headless=True keeps the demo from opening five simultaneous browser
    # windows when concurrency_limit=5 fires all tasks in parallel.
    agent_config = AgentConfig(
        starting_url="https://en.wikipedia.org/",
        api_key=api_key,
        allowed_domains=["en.wikipedia.org"],
        headless=False,
    )

    fleet_config = FleetConfig(
        concurrency_limit=5,
        agent_config=agent_config,
        deployment_pattern=DeploymentPattern.ECS,
        hitl_enabled=True,
    )

    orchestrator = FleetOrchestrator(config=fleet_config)

    # Batch of five independent article-reading tasks. Each task points
    # at a specific Wikipedia article and asks for the first sentence
    # of the introduction — a piece of content that exists on every
    # Wikipedia article, so the prompt shape is the same across the batch
    # even though the input URL (and therefore the returned data) varies.
    articles = [
        "Amazon_Web_Services",
        "Cloud_computing",
        "Kubernetes",
        "Docker_(software)",
        "Serverless_computing",
    ]

    tasks = [
        TaskDefinition(
            task_id=f"batch-wiki-{i:03d}",
            task_type="custom",
            starting_url=f"https://en.wikipedia.org/wiki/{article}",
            prompt=(
                "You are on a Wikipedia article page. Return the first "
                "sentence of the article's introduction (the first "
                "sentence of the first paragraph immediately below the "
                "article title, before the table of contents)."
            ),
        )
        for i, article in enumerate(articles, start=1)
    ]

    # Submit the batch
    batch_result = orchestrator.submit_batch(tasks)

    print(f"Batch ID:       {batch_result.batch_id}")
    print(f"Total tasks:    {batch_result.total_tasks}")
    print(f"Succeeded:      {batch_result.succeeded}")
    print(f"Failed:         {batch_result.failed}")
    print(f"Escalated:      {batch_result.escalated}")
    print(f"Rejected:       {batch_result.rejected}")
    print(f"Success rate:   {batch_result.success_rate:.1%}")
    print(f"Duration:       {batch_result.duration_seconds:.2f}s")

    for tr in batch_result.task_results:
        print(
            f"  {tr.task_id}: {tr.status.value} "
            f"({tr.duration_seconds:.2f}s)"
        )
        if tr.output:
            print(f"      output: {tr.output}")
        if tr.error_details:
            for err in tr.error_details:
                print(f"      error:  [{err.error_type}] {err.message}")


if __name__ == "__main__":
    main()
