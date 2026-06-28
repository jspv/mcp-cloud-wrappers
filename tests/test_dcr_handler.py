"""Unit tests for the DCR bridge Lambda (infra/lambda/dcr/handler.py).

The handler creates its boto3 clients and reads its config from os.environ at
import time, so each test loads a fresh copy of the module inside an active
moto mock with the environment already populated (see the ``dcr`` fixture).
"""

import importlib.util
import json
import pathlib

import boto3
import pytest
from moto import mock_aws

HANDLER_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "infra" / "lambda" / "dcr" / "handler.py"
)

REGION = "us-east-1"
RESOURCE_SERVER_ID = "mcp-api"
TABLE_NAME = "dcr-registrations"


def _load_handler():
    spec = importlib.util.spec_from_file_location("dcr_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dcr(monkeypatch):
    """Fresh handler module backed by moto-mocked Cognito + DynamoDB."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)

    with mock_aws():
        cog = boto3.client("cognito-idp", region_name=REGION)
        pool_id = cog.create_user_pool(PoolName="test-pool")["UserPool"]["Id"]
        cog.create_resource_server(
            UserPoolId=pool_id,
            Identifier=RESOURCE_SERVER_ID,
            Name=RESOURCE_SERVER_ID,
            Scopes=[
                {"ScopeName": "read", "ScopeDescription": "read"},
                {"ScopeName": "write", "ScopeDescription": "write"},
            ],
        )

        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "client_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "client_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("USER_POOL_ID", pool_id)
        monkeypatch.setenv("HOSTED_UI_DOMAIN", "auth.example.com")
        monkeypatch.setenv("RESOURCE_SERVER_ID", RESOURCE_SERVER_ID)
        monkeypatch.setenv("REGION", REGION)
        monkeypatch.setenv("DCR_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("DCR_API_URL", "https://dcr.example.com")

        yield _load_handler()


def _register(mod, **body):
    event = {"httpMethod": "POST", "path": "/register", "body": json.dumps(body)}
    resp = mod.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


# --- Happy paths ---------------------------------------------------------

def test_public_registration_has_no_secret(dcr):
    code, body = _register(
        dcr,
        client_name="Hermes Agent",
        redirect_uris=["http://127.0.0.1:8765/callback"],
        token_endpoint_auth_method="none",
    )
    assert code == 201
    assert body["client_id"]
    assert body["client_secret"] == ""
    assert body["token_endpoint_auth_method"] == "none"


def test_confidential_registration_has_secret(dcr):
    code, body = _register(
        dcr,
        client_name="Web App",
        redirect_uris=["https://app.example.com/cb"],
    )
    assert code == 201
    assert body["client_secret"]
    assert body["token_endpoint_auth_method"] == "client_secret_basic"


def test_loopback_ip_and_ipv6_accepted(dcr):
    code, _ = _register(
        dcr,
        client_name="Native",
        redirect_uris=["http://127.0.0.1:51000/cb", "http://[::1]:51000/cb"],
        token_endpoint_auth_method="none",
    )
    assert code == 201


# --- #3: token_endpoint_auth_method is reflected faithfully --------------

def test_client_secret_post_is_echoed(dcr):
    code, body = _register(
        dcr,
        client_name="Web App",
        redirect_uris=["https://app.example.com/cb"],
        token_endpoint_auth_method="client_secret_post",
    )
    assert code == 201
    assert body["client_secret"]
    assert body["token_endpoint_auth_method"] == "client_secret_post"


def test_unknown_auth_method_falls_back_to_basic(dcr):
    code, body = _register(
        dcr,
        client_name="Web App",
        redirect_uris=["https://app.example.com/cb"],
        token_endpoint_auth_method="private_key_jwt",
    )
    assert code == 201
    assert body["token_endpoint_auth_method"] == "client_secret_basic"


# --- Validation ----------------------------------------------------------

def test_non_loopback_http_rejected(dcr):
    code, body = _register(
        dcr, client_name="X", redirect_uris=["http://example.com/cb"],
    )
    assert code == 400
    assert body["error"] == "invalid_client_metadata"


def test_localhost_subdomain_spoof_rejected(dcr):
    # The old startswith("http://localhost") check would have allowed this.
    code, body = _register(
        dcr, client_name="X", redirect_uris=["http://localhost.evil.com/cb"],
    )
    assert code == 400
    assert body["error"] == "invalid_client_metadata"


# --- #4: non-string redirect_uri elements yield a clean 400 --------------

def test_non_string_redirect_uri_rejected(dcr):
    code, body = _register(dcr, client_name="X", redirect_uris=[123])
    assert code == 400
    assert body["error"] == "invalid_client_metadata"


# --- Idempotency ---------------------------------------------------------

def test_repeat_registration_is_idempotent(dcr):
    args = dict(
        client_name="Hermes Agent",
        redirect_uris=["http://127.0.0.1:8765/callback"],
        token_endpoint_auth_method="none",
    )
    code1, body1 = _register(dcr, **args)
    code2, body2 = _register(dcr, **args)
    assert code1 == 201
    assert code2 == 200
    assert body1["client_id"] == body2["client_id"]
    assert body2["client_secret"] == ""
    assert body2["token_endpoint_auth_method"] == "none"


# --- #2: a public re-registration supersedes a stale confidential row ----

def test_public_reregistration_supersedes_confidential(dcr):
    redirects = ["http://127.0.0.1:8765/callback"]

    code1, conf = _register(dcr, client_name="Hermes Agent", redirect_uris=redirects)
    assert code1 == 201
    assert conf["client_secret"]  # confidential

    code2, pub = _register(
        dcr,
        client_name="Hermes Agent",
        redirect_uris=redirects,
        token_endpoint_auth_method="none",
    )
    # A fresh public client is minted (201), not the stale confidential one (200).
    assert code2 == 201
    assert pub["client_id"] != conf["client_id"]
    assert pub["client_secret"] == ""
    assert pub["token_endpoint_auth_method"] == "none"

    # The original confidential client is still reachable for confidential reqs.
    code3, again = _register(dcr, client_name="Hermes Agent", redirect_uris=redirects)
    assert code3 == 200
    assert again["client_id"] == conf["client_id"]
    assert again["client_secret"]


# --- #1: Cognito rejection becomes a clean RFC 400, not a 500 ------------

def test_cognito_rejection_maps_to_400(dcr, monkeypatch):
    def _raise(**kwargs):
        raise dcr.cognito.exceptions.InvalidParameterException(
            {"Error": {
                "Code": "InvalidParameterException",
                "Message": "Invalid redirect URI",
            }},
            "CreateUserPoolClient",
        )

    monkeypatch.setattr(dcr.cognito, "create_user_pool_client", _raise)
    code, body = _register(
        dcr, client_name="X", redirect_uris=["https://app.example.com/cb"],
    )
    assert code == 400
    assert body["error"] == "invalid_redirect_uri"
    assert "Invalid redirect URI" in body["error_description"]


# --- Metadata ------------------------------------------------------------

def test_metadata_advertises_public_auth_method(dcr):
    event = {
        "httpMethod": "GET",
        "path": "/.well-known/oauth-authorization-server",
    }
    resp = dcr.handler(event, None)
    body = json.loads(resp["body"])
    assert "none" in body["token_endpoint_auth_methods_supported"]
    assert body["registration_endpoint"].endswith("/register")
