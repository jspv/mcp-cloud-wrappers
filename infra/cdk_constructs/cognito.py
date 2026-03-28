"""Cognito User Pool construct for MCP service authentication.

Adapted from glidepath's GlidepathCognitoStack — stripped of
glidepath-specific concerns (admin group, frontend client) and
generalized for the MCP wrapper framework.
"""

from __future__ import annotations

import boto3 as _boto3

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    SecretValue,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_ssm as ssm,
)
from constructs import Construct


def _ensure_apex_a_record(hosted_zone_name: str) -> None:
    """Create a placeholder A record for the apex if Cognito custom domain needs it."""
    r53 = _boto3.client("route53")
    resp = r53.list_hosted_zones_by_name(DNSName=hosted_zone_name, MaxItems="1")
    zones = [
        z for z in resp["HostedZones"]
        if z["Name"].rstrip(".") == hosted_zone_name.rstrip(".")
    ]
    if not zones:
        return
    zone_id = zones[0]["Id"].split("/")[-1]
    rrs = r53.list_resource_record_sets(
        HostedZoneId=zone_id,
        StartRecordName=hosted_zone_name,
        StartRecordType="A",
        MaxItems="1",
    )
    for record in rrs["ResourceRecordSets"]:
        if (
            record["Name"].rstrip(".") == hosted_zone_name.rstrip(".")
            and record["Type"] == "A"
        ):
            return
    r53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": hosted_zone_name,
                    "Type": "A",
                    "TTL": 300,
                    "ResourceRecords": [{"Value": "8.8.8.8"}],
                },
            }]
        },
    )


class CognitoPool(Construct):
    """Cognito User Pool with resource server and optional custom domain.

    Exposes ``user_pool``, ``hosted_ui_domain``, and
    ``resource_server_identifier`` for downstream constructs.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # ---- User Pool ----
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{prefix}-user-pool",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
        )

        # ---- Resource Server ----
        read_scope = cognito.ResourceServerScope(
            scope_name="read",
            scope_description="Read-only access to MCP services",
        )
        write_scope = cognito.ResourceServerScope(
            scope_name="write",
            scope_description="Write access to MCP services",
        )

        self.resource_server = self.user_pool.add_resource_server(
            "ResourceServer",
            identifier=prefix,
            scopes=[read_scope, write_scope],
        )
        self.resource_server_identifier = prefix

        # ---- Optional Social Federation ----
        identity_providers: list[cognito.UserPoolClientIdentityProvider] = [
            cognito.UserPoolClientIdentityProvider.COGNITO,
        ]

        google_client_id_ssm = self.node.try_get_context("google_client_id_ssm") or ""
        google_client_secret_ssm = self.node.try_get_context("google_client_secret_ssm") or ""

        if google_client_id_ssm and google_client_secret_ssm:
            cognito.UserPoolIdentityProviderGoogle(
                self,
                "GoogleProvider",
                user_pool=self.user_pool,
                client_id=ssm.StringParameter.value_from_lookup(
                    self, google_client_id_ssm
                ),
                client_secret_value=SecretValue.ssm_secure(google_client_secret_ssm),
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.GOOGLE_EMAIL,
                ),
            )
            identity_providers.append(
                cognito.UserPoolClientIdentityProvider.GOOGLE
            )

        # ---- Custom Domain ----
        domain_name = self.node.try_get_context("domain_name") or ""
        hosted_zone_name = self.node.try_get_context("hosted_zone_name") or ""

        if domain_name and hosted_zone_name:
            hosted_zone = route53.HostedZone.from_lookup(
                self, "HostedZone", domain_name=hosted_zone_name
            )
            _ensure_apex_a_record(hosted_zone_name)

            certificate = acm.Certificate(
                self,
                "AuthCertificate",
                domain_name=domain_name,
                validation=acm.CertificateValidation.from_dns(hosted_zone),
            )

            user_pool_domain = self.user_pool.add_domain(
                "AuthDomain",
                custom_domain=cognito.CustomDomainOptions(
                    domain_name=domain_name,
                    certificate=certificate,
                ),
            )

            route53.ARecord(
                self,
                "AuthDomainAlias",
                zone=hosted_zone,
                record_name=domain_name,
                target=route53.RecordTarget.from_alias(
                    targets.UserPoolDomainTarget(user_pool_domain)
                ),
            )
            self.hosted_ui_domain = domain_name
        else:
            self.user_pool.add_domain(
                "AuthDomain",
                cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix),
            )
            from aws_cdk import Stack

            self.hosted_ui_domain = (
                f"{prefix}.auth.{Stack.of(self).region}.amazoncognito.com"
            )

        # ---- Outputs ----
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolArn", value=self.user_pool.user_pool_arn)
        CfnOutput(self, "HostedUiDomain", value=self.hosted_ui_domain)
