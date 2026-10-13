"""SecurityManager for Nova Act Fleet.

Implements the application-side layers of the defense-in-depth model:

1. Input filtering        -- ``filter_prompt`` (called before every session
                              and every step prompt).
2. Domain restrictions    -- ``validate_domain`` (pre-flight on the starting
                              URL, and on every observed URL during a session
                              through the Nova Act SDK ``state_guardrail``).
3. Tool surface           -- no custom tools are registered with the Nova Act
                              SDK; ``registered_tools`` is an opt-in allowlist.
4. File access control    -- ``validate_file_access`` / ``allowed_file_paths``
                              (enforced by the SDK through ``SecurityOptions``).

Layers 5 (VPC isolation) and 6 (least-privilege IAM) are enforced by
``templates/ecs_deployment.yaml``, not by this module.
"""

import os
import re
from typing import Dict, List
from urllib.parse import urlparse

from nova_act_fleet.models.config import DeploymentPattern, SecurityConfig
from nova_act_fleet.models.domain import PromptFilterResult


class SecurityManager:
    """Enforces the application-side security controls.

    Applies input filtering, domain validation, and file access control,
    and exposes a starter IAM policy helper for non-CloudFormation
    deployments, based on the provided SecurityConfig.
    """

    def __init__(self, config: SecurityConfig) -> None:
        self._config = config
        self._compiled_patterns = [
            re.compile(p, re.IGNORECASE) for p in config.input_filter_patterns
        ]

    def filter_prompt(self, prompt: str) -> PromptFilterResult:
        """Filter a natural language prompt against configured patterns.

        Checks the prompt against all input_filter_patterns using regex
        matching. If any pattern matches, the prompt is blocked.

        Args:
            prompt: The natural language prompt to filter.

        Returns:
            PromptFilterResult indicating whether the prompt is allowed
            or blocked, with a reason and confidence score.
        """
        for pattern in self._compiled_patterns:
            if pattern.search(prompt):
                return PromptFilterResult(
                    allowed=False,
                    blocked_reason=f"Prompt matched blocked pattern: {pattern.pattern}",
                    confidence=1.0,
                )
        return PromptFilterResult(allowed=True, confidence=1.0)

    def validate_domain(self, url: str, allowlist: List[str], blocklist: List[str]) -> bool:
        """Validate a URL's domain against allowlist and blocklist.

        The domain must be present in the allowlist AND not present in
        the blocklist. Blocklist takes precedence over allowlist.

        Args:
            url: The URL to validate.
            allowlist: List of allowed domains.
            blocklist: List of blocked domains.

        Returns:
            True if the domain is allowed, False otherwise.
        """
        parsed = urlparse(url)
        domain = parsed.hostname or ""

        if domain in blocklist:
            return False

        if domain in allowlist:
            return True

        return False

    def validate_file_access(self, path: str) -> bool:
        """Validate whether the browser may open or upload ``path``.

        Returns False whenever ``file_access_enabled`` is False (the
        default). When enabled, the resolved path must equal, or sit under,
        one of ``allowed_file_paths``.

        Args:
            path: The file path to validate.

        Returns:
            True only if file access is enabled and ``path`` is allowlisted.
        """
        if not self._config.file_access_enabled:
            return False
        candidate = os.path.realpath(path)
        for allowed in self._config.allowed_file_paths:
            root = os.path.realpath(allowed)
            if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False

    def allowed_file_paths(self) -> List[str]:
        """Return the file paths to pass to the Nova Act SDK ``SecurityOptions``.

        Returns an empty list (all file access blocked) unless
        ``file_access_enabled`` is True.
        """
        if not self._config.file_access_enabled:
            return []
        return list(self._config.allowed_file_paths)

    def get_registered_tools(self) -> List[str]:
        """Return the opt-in allowlist of custom tool names.

        The framework itself registers no custom tools with the Nova Act
        SDK. Adopters that add tools should check each one against this
        list before passing it to ``NovaAct(tools=...)``.

        Returns:
            List of explicitly approved tool names.
        """
        return list(self._config.registered_tools)

    def get_iam_policy(self, pattern: DeploymentPattern) -> Dict:
        """Return a starter IAM policy template for the given deployment pattern.

        .. warning::

           The returned policy is a **starter template**, not a
           least-privilege policy. Every statement uses ``Resource: "*"``
           because the concrete ARNs (Lambda function, ECS task, ECR
           repository, Bedrock model) are not known to this helper at
           policy-generation time. Callers **must** narrow each statement's
           ``Resource`` (and, where appropriate, add ``Condition`` blocks)
           to the specific ARNs their deployment uses before attaching the
           policy to any role.

           For AWS CloudFormation deployments, prefer the fully scoped
           inline policies on ``ECSTaskExecutionRole`` and ``ECSTaskRole``
           in ``templates/ecs_deployment.yaml`` over this helper. This
           method exists for non-CFN, imperative deployments that build
           their IAM out of application code and want a starting point
           for the shape of the required permissions.

           See ``docs/security_guide.md`` for narrowing guidance and
           example scoped resources per pattern.

        Each returned policy covers only the API actions the corresponding
        execution environment needs; the responsibility for scoping
        ``Resource`` and (optionally) adding conditions rests with the
        caller.

        Args:
            pattern: The deployment pattern to generate a policy for.

        Returns:
            A dict representing an IAM policy document with wildcard
            resources that the caller must narrow before use.
        """
        base_statements = []

        if pattern == DeploymentPattern.LAMBDA:
            base_statements = [
                {
                    "Effect": "Allow",
                    "Action": [
                        "lambda:InvokeFunction",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:CreateNetworkInterface",
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DeleteNetworkInterface",
                    ],
                    "Resource": "*",
                },
            ]
        elif pattern == DeploymentPattern.ECS:
            base_statements = [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecs:RunTask",
                        "ecs:StopTask",
                        "ecs:DescribeTasks",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                        "ecr:GetAuthorizationToken",
                    ],
                    "Resource": "*",
                },
            ]
        elif pattern == DeploymentPattern.AGENTCORE:
            base_statements = [
                {
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:InvokeModel",
                    ],
                    "Resource": "*",
                },
            ]

        return {
            "Version": "2012-10-17",
            "Statement": base_statements,
        }
