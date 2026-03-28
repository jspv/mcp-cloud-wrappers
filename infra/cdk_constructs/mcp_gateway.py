"""AgentCore Gateway construct — MCP protocol endpoint with JWT auth.

Creates:
- IAM role for AgentCore to invoke the MCP Lambda
- CfnGateway with CUSTOM_JWT authorizer
- CfnGatewayTarget pointing at the Lambda

Tool definitions are read from ``tools.json`` in the service directory.
"""

from __future__ import annotations

import os
from typing import Any

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    aws_bedrockagentcore as ac,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

_HERE = os.path.dirname(os.path.abspath(__file__))


def _build_tool_definitions(
    tools: list[dict[str, Any]],
) -> list[ac.CfnGatewayTarget.ToolDefinitionProperty]:
    """Convert a list of tool dicts (from tools.json) to CDK properties."""
    definitions = []
    for tool in tools:
        schema = tool.get("inputSchema", {"type": "object", "properties": {}})
        definitions.append(
            ac.CfnGatewayTarget.ToolDefinitionProperty(
                name=tool["name"],
                description=tool.get("description", ""),
                input_schema=ac.CfnGatewayTarget.SchemaDefinitionProperty(
                    type=schema.get("type", "object"),
                    properties=schema.get("properties", {}),
                    required=schema.get("required", []),
                ),
            )
        )
    return definitions


class McpAgentCoreGateway(Construct):
    """AgentCore Gateway with MCP protocol support."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        service_name: str,
        lambda_function: lambda_.Function,
        discovery_url: str,
        tool_definitions: list[dict[str, Any]] | None = None,
        mcp_versions: list[str] | None = None,
        allowed_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        versions = mcp_versions or ["2025-03-26"]
        scopes = allowed_scopes or [f"{prefix}/read", f"{prefix}/write"]

        # Fall back to a ping tool if no definitions provided.
        tools = tool_definitions or [
            {"name": "ping", "description": "Health check."}
        ]

        # ---- Gateway execution role ----
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description=(
                f"Allows AgentCore Gateway to invoke the {service_name} MCP Lambda"
            ),
        )
        lambda_function.grant_invoke(gateway_role)

        # ---- Interceptor Lambda ----
        interceptor_fn = lambda_.Function(
            self,
            "InterceptorFn",
            function_name=f"{prefix}-{service_name}-interceptor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                os.path.join(_HERE, "..", "lambda", "interceptor")
            ),
            timeout=Duration.seconds(5),
            memory_size=128,
        )
        interceptor_fn.grant_invoke(gateway_role)

        # ---- AgentCore Gateway ----
        gateway = ac.CfnGateway(
            self,
            "McpGateway",
            name=f"{prefix}-{service_name}-gateway",
            protocol_type="MCP",
            authorizer_type="CUSTOM_JWT",
            role_arn=gateway_role.role_arn,
            authorizer_configuration=ac.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=ac.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_scopes=scopes,
                ),
            ),
            protocol_configuration=ac.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=ac.CfnGateway.MCPGatewayConfigurationProperty(
                    supported_versions=versions,
                ),
            ),
            interceptor_configurations=[
                ac.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST"],
                    interceptor=ac.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=ac.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=interceptor_fn.function_arn,
                        ),
                    ),
                    input_configuration=ac.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True,
                    ),
                )
            ],
        )

        # ---- Gateway Target ----
        gateway_target = ac.CfnGatewayTarget(
            self,
            "McpGatewayTarget",
            name=f"{prefix}-{service_name}-target",
            gateway_identifier=gateway.attr_gateway_identifier,
            credential_provider_configurations=[
                ac.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
            target_configuration=ac.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=ac.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=ac.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=lambda_function.function_arn,
                        tool_schema=ac.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=_build_tool_definitions(tools),
                        ),
                    )
                )
            ),
        )
        gateway_target.add_dependency(gateway)

        # ---- Outputs ----
        # Construct the URL from the gateway identifier — attr_gateway_url
        # is not reliably returned by CloudFormation on updates.
        from aws_cdk import Fn, Stack

        self.gateway_url = Fn.sub(
            "https://${GwId}.gateway.bedrock-agentcore.${Region}.amazonaws.com/mcp",
            {
                "GwId": gateway.attr_gateway_identifier,
                "Region": Stack.of(self).region,
            },
        )

        CfnOutput(
            self,
            "GatewayUrl",
            value=self.gateway_url,
            description=f"AgentCore Gateway URL for {service_name}",
        )
