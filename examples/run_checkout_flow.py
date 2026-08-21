#!/usr/bin/env python3
"""Demo: Drive a real checkout flow on https://www.amazon.com/.

This example uses ``WorkflowAgent.run_checkout_flow`` to walk Amazon's
public retail site through a realistic pre-purchase flow:

    1. dismiss any location / cookie / region interstitial
    2. search for a low-cost product
    3. open the first result
    4. add the item to the cart
    5. open the cart
    6. click "Proceed to checkout"

Step 6 lands on Amazon's sign-in page, which is the intentional stopping
point for this demo. No credentials, shipping address, or payment
information are entered at any point. The framework's per-step
screenshot capture still fires along the way, so you get evidence for
every stage of the flow.

Prerequisites:
    - pip install nova-act nova-act-fleet (or `pip install -e ".[dev]"`
      from the project root)
    - `python -m playwright install chromium`
    - NOVA_ACT_API_KEY in a `.env` file at the project root, or exported
      in your shell. Get a key at https://nova.amazon.com/act?tab=dev_tools
    - Optional: AWS credentials for CloudWatch/S3. Without them the
      framework degrades gracefully and just skips remote writes.

Environment overrides:
    NOVA_ACT_HEADLESS    "true"/"false" (default: false, so the demo is
                         actually visible in a browser window)
    DEMO_SEARCH_QUERY    override the search term (default: a cheap
                         USB-C cable)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from nova_act_fleet.agent import (
    AgentConfig,
    CheckoutFlowDefinition,
    CheckoutStep,
    WorkflowAgent,
)

load_dotenv()

AMAZON_START_URL = "https://www.amazon.com/"
DEFAULT_SEARCH_QUERY = "AmazonBasics USB-C to USB-A cable 6 foot"


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean-looking environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def build_flow(search_query: str) -> CheckoutFlowDefinition:
    """Build the multi-step Amazon checkout demo flow.

    The prompts are intentionally conservative:

    - Every step is a small, verifiable UI action the model can complete
      inside its default step budget.
    - We tell the model to dismiss warranty / protection-plan up-sells
      so the "Add to Cart" step doesn't stall on a popup.
    - The final step stops at the sign-in page and explicitly instructs
      the model not to sign in or enter any personal information.
    """
    return CheckoutFlowDefinition(
        flow_id="amazon-checkout-demo-001",
        starting_url=AMAZON_START_URL,
        step_wait_seconds=3.0,  # Amazon can be slow between navigations
        steps=[
            CheckoutStep(
                step_index=0,
                step_name="dismiss_interstitials",
                prompt=(
                    "You are on the Amazon.com homepage. If a location, "
                    "delivery-address, cookie-consent, or region-selection "
                    "popup is visible, dismiss it (choose 'Dismiss', "
                    "'Continue', or the close button). If no popup is "
                    "visible, do nothing and finish."
                ),
            ),
            CheckoutStep(
                step_index=1,
                step_name="search_product",
                prompt=(
                    f"Type '{search_query}' into the search bar at the top "
                    "of the page and submit the search."
                ),
            ),
            CheckoutStep(
                step_index=2,
                step_name="open_first_result",
                prompt=(
                    "You are on the Amazon search-results page. Click the "
                    "first organic (non-sponsored) product listing to "
                    "open its detail page. Wait until the product detail "
                    "page is fully loaded."
                ),
            ),
            CheckoutStep(
                step_index=3,
                step_name="add_to_cart",
                prompt=(
                    "You are on an Amazon product detail page. Click the "
                    "'Add to Cart' button. If a warranty, protection-plan, "
                    "or subscribe-and-save up-sell popup appears, decline "
                    "it (choose 'No thanks' or the close button)."
                ),
            ),
            CheckoutStep(
                step_index=4,
                step_name="open_cart",
                prompt=(
                    "Open the shopping cart by clicking the cart icon in "
                    "the top-right corner of the page. Wait until the "
                    "cart page is fully loaded and the line item is "
                    "visible."
                ),
            ),
            CheckoutStep(
                step_index=5,
                step_name="proceed_to_checkout",
                prompt=(
                    "You are on the Amazon shopping-cart page. Click the "
                    "yellow 'Proceed to checkout' button. As soon as the "
                    "next page loads (this will be the Amazon sign-in "
                    "page), STOP. Do not sign in. Do not click 'Continue'. "
                    "Do not enter an email address, phone number, "
                    "password, shipping address, or payment information."
                ),
            ),
        ],
    )


def main() -> None:
    api_key = os.environ.get("NOVA_ACT_API_KEY")
    if not api_key:
        raise SystemExit(
            "NOVA_ACT_API_KEY is not set. Add it to a .env file at the "
            "project root or export it in your shell. Get a key at "
            "https://nova.amazon.com/act?tab=dev_tools"
        )

    headless = _env_bool("NOVA_ACT_HEADLESS", default=False)
    search_query = os.environ.get("DEMO_SEARCH_QUERY", DEFAULT_SEARCH_QUERY)

    # Only the starting hostname needs to be allow-listed. The framework
    # validates the domain once, before the NovaAct session opens (see
    # SecurityManager.validate_domain: exact-hostname match). Once the
    # session is running, Nova Act follows in-site navigation (search,
    # product page, cart, sign-in) without re-checking the allowlist.
    config = AgentConfig(
        starting_url=AMAZON_START_URL,
        api_key=api_key,
        allowed_domains=["www.amazon.com"],
        headless=headless,
        session_timeout_seconds=1800,
        max_steps=100,
        max_prompt_length=10000,
    )

    agent = WorkflowAgent(config=config)
    flow = build_flow(search_query)

    print(f"Running Amazon checkout demo (headless={headless})")
    print(f"  Search query: {search_query}")
    print(f"  Flow ID:      {flow.flow_id}")
    print(f"  Steps:        {len(flow.steps)}")
    print()

    result = agent.run_checkout_flow(flow)

    print("=" * 60)
    print(f"Flow ID:          {result.flow_id}")
    print(f"Status:           {result.status.value}")
    print(f"Completed steps:  {result.completed_steps}/{result.total_steps}")
    print()

    for sr in result.step_results:
        status_icon = "✓" if sr.status.value == "success" else "✗"
        print(
            f"  {status_icon} Step {sr.step_index} ({sr.step_name}): "
            f"{sr.status.value}"
        )
        if sr.page_url:
            print(f"      URL:        {sr.page_url}")
        if sr.screenshot_key:
            print(f"      Screenshot: {sr.screenshot_key}")
        if sr.error:
            print(f"      Error:      [{sr.error.error_type}] {sr.error.message}")

    print()
    if result.status.value == "success":
        print(
            "Demo complete. The agent stopped on Amazon's sign-in page. "
            "No credentials, address, or payment info were submitted."
        )
    else:
        print(
            "Demo halted before completion. Inspect the failing step "
            "above and its screenshot for context. Amazon's UI varies "
            "by region and A/B test, so an occasional step failure is "
            "expected. Re-run to retry."
        )


if __name__ == "__main__":
    main()
