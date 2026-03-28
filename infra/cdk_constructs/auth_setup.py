"""Auth setup construct — web page for connecting external OAuth services.

Creates:
- Cognito app client (for the auth page's login flow)
- Auth setup Lambda
- API Gateway routes (/auth/setup, /auth/callback, /auth/connect/*, /auth/status)
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_apigateway as apigw,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from .bundler import LocalPipBundler

_HERE = os.path.dirname(os.path.abspath(__file__))


class AuthSetup(Construct):
    """Web page for users to connect external OAuth services."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        api: apigw.RestApi,
        user_pool: cognito.IUserPool,
        user_pool_id: str,
        hosted_ui_domain: str,
        oauth_callback_url: str,
        oauth_state_table_name: str,
        service_oauth_configs: str,
    ) -> None:
        super().__init__(scope, construct_id)

        stack = cdk.Stack.of(self)

        # ---- URLs (from API Gateway ID to avoid circular deps) ----
        auth_callback_url = cdk.Fn.sub(
            "https://${ApiId}.execute-api.${Region}.amazonaws.com/prod/auth/callback",
            {"ApiId": api.rest_api_id, "Region": stack.region},
        )
        self.auth_setup_url = cdk.Fn.sub(
            "https://${ApiId}.execute-api.${Region}.amazonaws.com/prod/auth/setup",
            {"ApiId": api.rest_api_id, "Region": stack.region},
        )

        # ---- Cognito app client ----
        auth_client = user_pool.add_client(
            "AuthSetupClient",
            user_pool_client_name=f"{prefix}-auth-setup",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                ],
                callback_urls=[auth_callback_url],
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            prevent_user_existence_errors=True,
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO,
            ],
        )

        # ---- IAM role ----
        role = iam.Role(
            self,
            "AuthSetupRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}"
                f":secret:{prefix}-*",
            ],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"],
            resources=[
                f"arn:aws:dynamodb:{stack.region}:{stack.account}"
                f":table/{oauth_state_table_name}",
            ],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["cognito-idp:DescribeUserPoolClient"],
            resources=[user_pool.user_pool_arn],
        ))

        # ---- Lambda ----
        lambda_src = os.path.join(_HERE, "..", "lambda", "auth_setup")
        auth_lambda = lambda_.Function(
            self,
            "AuthSetupLambda",
            function_name=f"{prefix}-auth-setup",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                lambda_src,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=LocalPipBundler(lambda_src),
                    command=[
                        "bash", "-c",
                        "pip install --no-cache-dir -r requirements.txt -t /asset-output && "
                        "cp -au . /asset-output",
                    ],
                ),
            ),
            role=role,
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                "COGNITO_DOMAIN": hosted_ui_domain,
                "COGNITO_CLIENT_ID": auth_client.user_pool_client_id,
                "COGNITO_USER_POOL_ID": user_pool_id,
                "AUTH_CALLBACK_URL": auth_callback_url,
                "AUTH_SETUP_URL": self.auth_setup_url,
                "OAUTH_CALLBACK_URL": oauth_callback_url,
                "OAUTH_STATE_TABLE": oauth_state_table_name,
                "SECRET_PREFIX": prefix,
                "SERVICE_OAUTH_CONFIGS": service_oauth_configs,
            },
        )

        # ---- API Gateway routes ----
        integration = apigw.LambdaIntegration(auth_lambda, proxy=True)
        auth_resource = api.root.add_resource("auth")
        auth_resource.add_resource("setup").add_method("GET", integration)
        auth_resource.add_resource("callback").add_method("GET", integration)
        auth_resource.add_resource("status").add_method("GET", integration)
        connect_resource = auth_resource.add_resource("connect")
        connect_resource.add_resource("{service}").add_method("GET", integration)

        # ---- Output ----
        cdk.CfnOutput(self, "AuthSetupUrl", value=self.auth_setup_url,
                       description="Auth setup page URL")
