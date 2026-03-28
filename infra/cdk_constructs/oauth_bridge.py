"""OAuth Bridge construct — generic OAuth callback for all providers.

Creates:
- DynamoDB table for pending OAuth state (with TTL)
- OAuth callback Lambda
- Adds /oauth/callback route to an existing API Gateway

The callback handler is completely provider-agnostic — it reads the
token_endpoint and client credentials from the DynamoDB state record
that was stored when the auth URL was built.
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


class OAuthBridge(Construct):
    """Generic OAuth2 callback handler with DynamoDB state management."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        api: apigw.RestApi,
        secret_prefix: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        resolved_secret_prefix = secret_prefix or prefix

        # ---- DynamoDB table for pending OAuth flows ----
        self.state_table = dynamodb.Table(
            self,
            "OAuthStateTable",
            table_name=f"{prefix}-oauth-state",
            partition_key=dynamodb.Attribute(
                name="state",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- IAM role ----
        callback_role = iam.Role(
            self,
            "OAuthCallbackRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        self.state_table.grant_read_write_data(callback_role)

        # Callback needs to read service secrets (for client_secret) and
        # create/update per-user secrets.
        callback_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:CreateSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{cdk.Stack.of(self).region}:"
                    f"{cdk.Stack.of(self).account}:secret:{resolved_secret_prefix}-*",
                ],
            )
        )

        # ---- Callback URL ----
        # Build from the API ID + region directly to avoid a circular
        # dependency (api.url depends on the deployment stage, which
        # depends on routes, which depend on this Lambda).
        self.callback_url = cdk.Fn.sub(
            "https://${ApiId}.execute-api.${Region}.amazonaws.com/prod/oauth/callback",
            {
                "ApiId": api.rest_api_id,
                "Region": cdk.Stack.of(self).region,
            },
        )

        # ---- Lambda ----
        callback_src = os.path.join(_HERE, "..", "lambda", "oauth_callback")
        callback_lambda = lambda_.Function(
            self,
            "OAuthCallbackLambda",
            function_name=f"{prefix}-oauth-callback",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                callback_src,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=LocalPipBundler(callback_src),
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
                    ],
                ),
            ),
            role=callback_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                "OAUTH_STATE_TABLE": self.state_table.table_name,
                "OAUTH_CALLBACK_URL": self.callback_url,
                "SECRET_PREFIX": resolved_secret_prefix,
            },
        )

        # ---- Add /oauth/callback route to shared API Gateway ----
        oauth_resource = api.root.add_resource("oauth")
        oauth_resource.add_resource("callback").add_method(
            "GET", apigw.LambdaIntegration(callback_lambda, proxy=True)
        )
