"""MCP Server Lambda construct — creates a per-service Lambda function.

The Lambda runs the McpServiceHandler which launches the MCP server
as a stdio subprocess via the mcp_lambda adapter.
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from .bundler import LocalPipBundler


class McpServerLambda(Construct):
    """Lambda function for a specific MCP service."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        service_name: str,
        handler_source_dir: str,
        timeout_seconds: int = 120,
        memory_mb: int = 512,
        environment: dict[str, str] | None = None,
        extra_policies: list[iam.PolicyStatement] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        # ---- IAM role ----
        self.role = iam.Role(
            self,
            "McpLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        for policy in extra_policies or []:
            self.role.add_to_policy(policy)

        # ---- Lambda function ----
        self.function = lambda_.Function(
            self,
            "McpServerLambda",
            function_name=f"{prefix}-{service_name}-mcp",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                handler_source_dir,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=LocalPipBundler(handler_source_dir),
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
                    ],
                ),
            ),
            role=self.role,
            timeout=Duration.seconds(timeout_seconds),
            memory_size=memory_mb,
            environment=environment or {},
        )
