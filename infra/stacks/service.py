"""Per-service stack — one for each wrapped MCP service.

Creates:
- MCP Server Lambda (runs the wrapped stdio MCP server)
- AgentCore Gateway + GatewayTarget (MCP protocol endpoint)
- IAM policies for Secrets Manager and OAuth state table access
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import CfnOutput, Stack, aws_dynamodb as dynamodb, aws_iam as iam
from constructs import Construct

from cdk_constructs import McpAgentCoreGateway, McpServerLambda


class ServiceStack(Stack):
    """Deploys one MCP service behind AgentCore Gateway."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        # Service identity
        service_name: str,
        handler_source_dir: str,
        # From SharedInfraStack
        discovery_url: str,
        oauth_callback_url: str,
        oauth_state_table_arn: str,
        oauth_state_table_name: str,
        # Lambda tuning
        lambda_timeout: int = 120,
        lambda_memory: int = 512,
        lambda_environment: dict[str, str] | None = None,
        auth_setup_url: str = "",
        # Tool definitions (from tools.json) for the AgentCore GatewayTarget
        tool_definitions: list[dict] | None = None,
        # Extra IAM policies beyond the defaults
        extra_policies: list[iam.PolicyStatement] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("prefix") or "mcp-wrappers"

        # ---- Build merged environment ----
        env_vars = {
            "SECRET_PREFIX": prefix,
            "SERVICE_NAME": service_name,
            "OAUTH_STATE_TABLE": oauth_state_table_name,
            "OAUTH_CALLBACK_URL": oauth_callback_url,
        }
        if auth_setup_url:
            env_vars["AUTH_SETUP_URL"] = auth_setup_url
        if lambda_environment:
            env_vars.update(lambda_environment)

        # ---- Default IAM policies ----
        policies = list(extra_policies or [])

        # Secrets Manager: read/write per-user and service secrets
        policies.append(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:CreateSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}"
                    f":secret:{prefix}-{service_name}-*",
                ],
            )
        )

        # DynamoDB: read/write OAuth state (for building auth URLs)
        policies.append(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[oauth_state_table_arn],
            )
        )

        # ---- MCP Server Lambda ----
        mcp_lambda = McpServerLambda(
            self,
            "McpLambda",
            prefix=prefix,
            service_name=service_name,
            handler_source_dir=handler_source_dir,
            timeout_seconds=lambda_timeout,
            memory_mb=lambda_memory,
            environment=env_vars,
            extra_policies=policies,
        )

        # ---- AgentCore Gateway ----
        gateway = McpAgentCoreGateway(
            self,
            "McpGateway",
            prefix=prefix,
            service_name=service_name,
            lambda_function=mcp_lambda.function,
            discovery_url=discovery_url,
            tool_definitions=tool_definitions,
        )

        # ---- Outputs ----
        CfnOutput(self, "McpLambdaArn", value=mcp_lambda.function.function_arn,
                  description=f"{service_name} MCP Server Lambda ARN")
        # GatewayUrl output is created by the McpAgentCoreGateway construct.
