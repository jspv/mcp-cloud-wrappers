"""AgentCore Gateway construct — MCP protocol endpoint with JWT auth.

Creates:
- IAM role for AgentCore to invoke the MCP Lambda
- CfnGateway with CUSTOM_JWT authorizer
- CfnGatewayTarget pointing at the Lambda
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    aws_bedrockagentcore as ac,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct


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
        mcp_versions: list[str] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        versions = mcp_versions or ["2024-11-05"]

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
                ),
            ),
            protocol_configuration=ac.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=ac.CfnGateway.MCPGatewayConfigurationProperty(
                    supported_versions=versions,
                ),
            ),
        )

        # ---- Gateway Target ----
        gateway_target = ac.CfnGatewayTarget(
            self,
            "McpGatewayTarget",
            name=f"{prefix}-{service_name}-target",
            gateway_identifier=gateway.attr_gateway_identifier,
            target_configuration=ac.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=ac.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=ac.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=lambda_function.function_arn,
                        tool_schema=ac.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[
                                ac.CfnGatewayTarget.ToolDefinitionProperty(
                                    name="ping",
                                    description=(
                                        "Health check — returns ok when the "
                                        "MCP server is running."
                                    ),
                                    input_schema=ac.CfnGatewayTarget.SchemaDefinitionProperty(
                                        type="object",
                                        properties={},
                                        required=[],
                                    ),
                                )
                            ]
                        ),
                    )
                )
            ),
        )
        gateway_target.add_dependency(gateway)

        # ---- Outputs ----
        self.gateway_url = gateway.attr_gateway_url

        CfnOutput(
            self,
            "GatewayUrl",
            value=gateway.attr_gateway_url,
            description=f"AgentCore Gateway URL for {service_name}",
        )
