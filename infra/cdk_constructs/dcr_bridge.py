"""DCR Bridge construct — RFC 7591 Dynamic Client Registration.

Creates:
- DynamoDB table for registration records
- DCR Lambda (OIDC metadata + /register endpoint)
- API Gateway with .well-known routes and /register

Extracted from glidepath's GlidepathMcpOAuthStack.
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_apigateway as apigw,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from .bundler import LocalPipBundler

_HERE = os.path.dirname(os.path.abspath(__file__))


class DcrBridge(Construct):
    """OIDC metadata + RFC 7591 Dynamic Client Registration bridge."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        user_pool_id: str,
        user_pool_arn: str,
        hosted_ui_domain: str,
        resource_server_identifier: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # ---- DynamoDB table ----
        self.dcr_table = dynamodb.Table(
            self,
            "DcrTable",
            table_name=f"{prefix}-mcp-dcr",
            partition_key=dynamodb.Attribute(
                name="client_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- API Gateway ----
        self.api = apigw.RestApi(
            self,
            "DcrApi",
            rest_api_name=f"{prefix}-mcp-dcr-api",
            description="DCR bridge: OIDC metadata + RFC 7591 registration + OAuth callback",
            deploy=True,
            deploy_options=apigw.StageOptions(stage_name="prod"),
        )
        # Build URL from API ID to avoid circular dependency with deployment stage.
        dcr_api_url = cdk.Fn.sub(
            "https://${ApiId}.execute-api.${Region}.amazonaws.com/prod",
            {
                "ApiId": self.api.rest_api_id,
                "Region": cdk.Stack.of(self).region,
            },
        )

        # ---- IAM role ----
        dcr_lambda_role = iam.Role(
            self,
            "DcrLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        dcr_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:CreateUserPoolClient",
                    "cognito-idp:DescribeUserPoolClient",
                    "cognito-idp:ListUserPoolClients",
                ],
                resources=[user_pool_arn],
            )
        )
        self.dcr_table.grant_read_write_data(dcr_lambda_role)

        # ---- Lambda ----
        dcr_lambda_src = os.path.join(_HERE, "..", "lambda", "dcr")
        dcr_lambda = lambda_.Function(
            self,
            "DcrLambda",
            function_name=f"{prefix}-mcp-dcr",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                dcr_lambda_src,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=LocalPipBundler(dcr_lambda_src),
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
                    ],
                ),
            ),
            role=dcr_lambda_role,
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "USER_POOL_ID": user_pool_id,
                "HOSTED_UI_DOMAIN": hosted_ui_domain,
                "RESOURCE_SERVER_ID": resource_server_identifier,
                "REGION": cdk.Stack.of(self).region,
                "DCR_TABLE_NAME": self.dcr_table.table_name,
                "DCR_API_URL": dcr_api_url,
            },
        )

        # ---- API Gateway routes ----
        dcr_integration = apigw.LambdaIntegration(dcr_lambda, proxy=True)
        well_known = self.api.root.add_resource(".well-known")
        well_known.add_resource("openid-configuration").add_method("GET", dcr_integration)
        well_known.add_resource("oauth-authorization-server").add_method("GET", dcr_integration)
        self.api.root.add_resource("register").add_method("POST", dcr_integration)

        # ---- Exposed properties ----
        self.discovery_url = cdk.Fn.sub(
            "${BaseUrl}/.well-known/openid-configuration",
            {"BaseUrl": dcr_api_url},
        )
        self.dcr_api_url = dcr_api_url
