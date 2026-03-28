"""Shared infrastructure stack — deployed once, used by all services.

Contains:
- Cognito User Pool (caller authentication)
- DCR Bridge (OIDC metadata + dynamic client registration)
- OAuth Bridge (callback endpoint for external provider auth)
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import CfnOutput, Stack
from constructs import Construct

from cdk_constructs import CognitoPool, DcrBridge, OAuthBridge, AuthSetup


class SharedInfraStack(Stack):
    """Shared infra: Cognito + DCR + OAuth callback.

    If ``external_user_pool_*`` parameters are provided, skips creating
    a new Cognito pool and uses the external one instead.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        # Optional: provide these to use an external Cognito pool
        external_user_pool_id: str | None = None,
        external_user_pool_arn: str | None = None,
        external_hosted_ui_domain: str | None = None,
        external_resource_server_identifier: str | None = None,
        service_oauth_configs: list[dict] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("prefix") or "mcp-wrappers"

        # ---- Cognito (create or use external) ----
        if external_user_pool_id:
            self.user_pool_id = external_user_pool_id
            self.user_pool_arn = external_user_pool_arn or ""
            self.hosted_ui_domain = external_hosted_ui_domain or ""
            self.resource_server_identifier = (
                external_resource_server_identifier or prefix
            )
        else:
            cognito_pool = CognitoPool(self, "Cognito", prefix=prefix)
            self.user_pool_id = cognito_pool.user_pool.user_pool_id
            self.user_pool_arn = cognito_pool.user_pool.user_pool_arn
            self.hosted_ui_domain = cognito_pool.hosted_ui_domain
            self.resource_server_identifier = cognito_pool.resource_server_identifier

        # ---- DCR Bridge ----
        dcr = DcrBridge(
            self,
            "DcrBridge",
            prefix=prefix,
            user_pool_id=self.user_pool_id,
            user_pool_arn=self.user_pool_arn,
            hosted_ui_domain=self.hosted_ui_domain,
            resource_server_identifier=self.resource_server_identifier,
        )
        self.discovery_url = dcr.discovery_url
        self.dcr_api_url = dcr.dcr_api_url

        # ---- OAuth Bridge (adds /oauth/callback to DCR API Gateway) ----
        oauth = OAuthBridge(
            self,
            "OAuthBridge",
            prefix=prefix,
            api=dcr.api,
            secret_prefix=prefix,
        )
        self.oauth_callback_url = oauth.callback_url
        self.oauth_state_table = oauth.state_table

        # ---- Auth Setup (web page for connecting external services) ----
        self.auth_setup_url = ""
        if service_oauth_configs:
            import json as _json
            # Determine user pool reference
            if external_user_pool_id:
                from aws_cdk import aws_cognito as cog
                _user_pool = cog.UserPool.from_user_pool_id(
                    self, "ImportedPool", external_user_pool_id
                )
            else:
                _user_pool = cognito_pool.user_pool

            auth = AuthSetup(
                self,
                "AuthSetup",
                prefix=prefix,
                api=dcr.api,
                user_pool=_user_pool,
                user_pool_id=self.user_pool_id,
                hosted_ui_domain=self.hosted_ui_domain,
                oauth_callback_url=oauth.callback_url,
                oauth_state_table_name=oauth.state_table.table_name,
                service_oauth_configs=_json.dumps(service_oauth_configs),
            )
            self.auth_setup_url = auth.auth_setup_url
            CfnOutput(self, "AuthSetupUrl", value=auth.auth_setup_url,
                      description="Auth setup page URL")

        # ---- Outputs ----
        CfnOutput(self, "DcrApiUrl", value=dcr.dcr_api_url,
                  description="DCR bridge API Gateway base URL")
        CfnOutput(self, "OAuthCallbackUrl", value=oauth.callback_url,
                  description="OAuth callback URL for external providers")
        CfnOutput(self, "OAuthStateTableName", value=oauth.state_table.table_name,
                  description="DynamoDB table for pending OAuth flows")
