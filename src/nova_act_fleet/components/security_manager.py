"""SecurityManager for Nova Act Fleet.

Enforces the six-layer defense-in-depth security architecture:
input filtering, domain restrictions, tool registration,
file access control, VPC isolation, and IAM permissions.
"""

import re
from typing import Dict, List
from urllib.parse import urlparse

from nova_act_fleet.models.config import DeploymentPattern, SecurityConfig
from nova_act_fleet.models.domain import PromptFilterResult


class SecurityManager:
    """Enforces defense-in-depth security controls.

    Applies input filtering, domain validation, tool registration,
    file access control, and least-privilege IAM policy generation
    based on the provided SecurityConfig.
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
        """Validate whether file access is permitted.

        Returns False by default when file_access_enabled is False,
        blocking all file system access from browser sessions.

        Args:
            path: The file path to validate.

        Returns:
            False when file_access_enabled is False, True otherwise.
        """
        return self._config.file_access_enabled

    def get_registered_tools(self) -> List[str]:
        """Return the configured list of registered tools.

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
