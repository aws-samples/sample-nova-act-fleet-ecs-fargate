# Container image for Nova Act Fleet workflow agents on Amazon ECS Fargate.
#
# This image bundles:
#   - Python 3.12
#   - The nova-act-fleet package (this repo, installed in editable mode from /app)
#   - Playwright Chromium and the system dependencies it needs
#
# The container reads a TaskDefinition payload from the TASK_PAYLOAD environment
# variable (or stdin), constructs a WorkflowAgent from the surrounding env, runs
# the task, prints the TaskResult as JSON to stdout, and exits.
#
# See docker/entrypoint.py for the runtime contract.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    NOVA_ACT_SKIP_PLAYWRIGHT_INSTALL=1

# System packages required by Playwright Chromium on Debian slim.
# The list mirrors what `playwright install-deps chromium` would install; we
# pin them explicitly so the image build is hermetic.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libgbm1 \
        libglib2.0-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        wget \
        xdg-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy package metadata first so dependency installs cache between builds.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Pin pip to a specific version so successive builds of this Dockerfile
# resolve to the same installer. The Python dependencies below and the
# base image tag are already pinned; this closes the last unpinned link
# in the chain.
RUN pip install --upgrade "pip==26.2.1" \
 && pip install .

# Install Chromium into the path declared above. Playwright honours
# PLAYWRIGHT_BROWSERS_PATH at install time and at runtime.
RUN python -m playwright install chromium

# Copy the runtime entrypoint last so iteration on it doesn't bust the
# expensive layers above.
COPY docker/entrypoint.py /app/docker/entrypoint.py

# Sanity check that the package and Chromium are importable / launchable.
RUN python -c "from nova_act_fleet.agent import WorkflowAgent, TaskDefinition" \
 && python -c "from playwright.sync_api import sync_playwright; \
import contextlib; \
p = sync_playwright().start(); \
print('chromium executable:', p.chromium.executable_path); \
p.stop()"

# Drop root before the container starts serving the workload. A Chromium
# sandbox escape or other in-container code execution should not land on
# UID 0. Ownership of /app and the Playwright browser install is handed to
# the runtime user so the entrypoint can read and (where needed) write to
# both trees.
RUN groupadd --gid 1000 appuser \
 && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser \
 && chown -R appuser:appuser /app /opt/playwright-browsers

USER appuser

# Lightweight liveness probe. Importing nova_act_fleet.agent exercises the
# Pydantic models and boto3 import graph the entrypoint depends on, so a
# failure here signals that the Python environment is broken (missing deps,
# corrupt install) rather than that the current task is slow.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import nova_act_fleet.agent" || exit 1

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
