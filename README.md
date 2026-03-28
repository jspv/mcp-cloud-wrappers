# MCP Lambda Wrappers

A reusable framework for deploying most any stdio-based MCP (Model Context Protocol) server as an AWS Lambda function behind [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html), with Cognito OAuth and Dynamic Client Registration.

You bring an MCP server package (yours, open source, wherever it lives). This framework provides the infrastructure to run it serverlessly: caller authentication, dynamic client registration, per-user OAuth token lifecycle, and the Lambda subprocess bridge. You write a `ServiceConfig` (a few lines of Python) and a `requirements.txt`.

## How it works

The framework wraps MCP servers that you develop and maintain **outside this repository**. Each MCP server is any program that speaks the MCP stdio protocol — Python packages (built with [FastMCP](https://github.com/jlowin/fastmcp), the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), etc.), Node.js servers, Go binaries, or anything else that reads/writes MCP JSON-RPC over stdin/stdout. The framework:

1. Bundles your MCP server into a Lambda deployment package
2. Launches it as a subprocess on each invocation
3. Bridges AgentCore Gateway events to the subprocess via `mcp_lambda`
4. Manages all authentication and credential injection automatically

Your MCP server doesn't need to know anything about Lambda, AgentCore, or Cognito. The only adaptation is a one-line check for a framework-injected access token environment variable (see [Preparing your MCP server](#1-prepare-your-mcp-server)).

## Architecture

```
                     ┌──────────────────────────────────────────┐
                     │         Shared Infrastructure            │
                     │         (deployed once)                  │
  MCP Client ──────► │  Cognito User Pool  (caller auth)       │
  (Claude, agent)    │  DCR API Gateway    (.well-known, /register)
                     │  OAuth Callback     (/oauth/callback)    │
                     │  DynamoDB tables    (DCR + OAuth state)  │
                     └───────────────┬──────────────────────────┘
                                     │
            ┌────────────────────────┼────────────────────────┐
            │                        │                        │
  ┌─────────▼──────────┐  ┌─────────▼──────────┐  ┌─────────▼──────────┐
  │  Service A         │  │  Service B         │  │  Service C         │
  │                    │  │                    │  │                    │
  │  AgentCore Gateway │  │  AgentCore Gateway │  │  AgentCore Gateway │
  │        │           │  │        │           │  │        │           │
  │  MCP Server Lambda │  │  MCP Server Lambda │  │  MCP Server Lambda │
  │   └─ subprocess:   │  │   └─ subprocess:   │  │   └─ subprocess:   │
  │     your_mcp_pkg   │  │     another_pkg    │  │     third_pkg      │
  └────────────────────┘  └────────────────────┘  └────────────────────┘
```

Each service gets its own Lambda + AgentCore Gateway. They share the Cognito pool, DCR endpoint, and OAuth callback infrastructure.

### Two layers of authentication

| Layer | Purpose | Mechanism |
|-------|---------|-----------|
| **Caller auth** | Who is calling the MCP gateway? | Cognito JWT validated by AgentCore Gateway |
| **Backend auth** | What can the service access on behalf of the user? | Standard OAuth2 (authorization code + PKCE) managed by the framework |

### Three categories of environment variables

Every wrapped service may need some combination of these. The framework loads all three categories and merges them into the subprocess environment automatically.

| Category | Example | Where it lives | Who manages it |
|----------|---------|-----------------|----------------|
| 1. Static config | `API_BASE_URL`, `LOG_LEVEL` | CDK context / Lambda env var | You, at deploy time |
| 2. Service secrets | `CLIENT_ID`, `CLIENT_SECRET`, `API_KEY` | Secrets Manager (one JSON object per service — each key becomes an env var) | You, created once |
| 3. Per-user credentials | Access token | Secrets Manager (one per user per service) | Framework, via OAuth flow |

Category 1 is for non-secret configuration. All credentials — including client IDs — belong in Category 2 (the service secret).

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js (CDK CLI runs on Node; the rest of the project is Python)
- AWS CLI configured with credentials
- CDK bootstrapped: `make bootstrap` (or see [Deployment](#deployment))

## Repository structure

```
mcp-lambda-wrappers/
├── packages/
│   └── mcp-wrapper-runtime/             # Framework runtime (installed into each Lambda)
│       └── src/mcp_wrapper/
│           ├── config.py                # ServiceConfig, OAuthProviderConfig
│           ├── credentials.py           # CredentialManager (Secrets Manager)
│           ├── oauth.py                 # OAuthHelper (PKCE, exchange, refresh)
│           └── handler.py              # McpServiceHandler (base Lambda handler)
│
├── infra/
│   ├── app.py                           # CDK app — defines all stacks
│   ├── cdk_constructs/                  # Reusable CDK constructs
│   │   ├── bundler.py                   # Local pip/uv bundler for Lambda assets
│   │   ├── cognito.py                   # Cognito User Pool + resource server
│   │   ├── dcr_bridge.py               # DCR Lambda + API Gateway
│   │   ├── oauth_bridge.py             # OAuth callback Lambda
│   │   ├── mcp_lambda.py               # Per-service MCP Lambda
│   │   └── mcp_gateway.py              # AgentCore Gateway + GatewayTarget
│   ├── stacks/
│   │   ├── shared.py                    # SharedInfraStack (deploy once)
│   │   └── service.py                   # ServiceStack (one per wrapped service)
│   └── lambda/
│       ├── dcr/                         # Shared: RFC 7591 Dynamic Client Registration
│       ├── oauth_callback/              # Shared: Generic OAuth2 callback (all providers)
│       └── services/
│           └── msgraph/                 # Example: Microsoft Graph wrapper
│               ├── handler.py           #   15 lines of config
│               └── requirements.txt     #   lists the MCP package + framework deps
│
├── scripts/verify_deployment.py         # Post-deploy smoke test
├── cdk.json
├── Makefile
└── pyproject.toml
```

## Wrapping a new MCP service — step by step

This section walks through the full process. The bundled `msgraph` wrapper is a concrete example of every step described here.

### 1. Prepare your MCP server

Your MCP server lives in its own repository, package registry, or wherever you keep it. It can be written in **any language** — the framework launches it as a subprocess and communicates over stdio.

If your service uses per-user OAuth (e.g., accessing a user's mailbox or calendar), you need to make one small change **in your MCP server's source code** — specifically, in the module that obtains an access token for API calls. Before running its own auth flow, it should check for a token the framework has already placed in the environment. This lets the same server code work both locally (where it runs its own auth) and inside Lambda (where the framework manages auth).

Find the place in your MCP server that acquires an access token, and add an env var check at the top:

**Python** — in your MCP server's auth or HTTP client module:
```python
import os

def get_token():
    # When running inside Lambda, the framework injects a valid token
    token = os.environ.get("MY_SERVICE_ACCESS_TOKEN")
    if token:
        return token
    # When running locally, use the normal auth flow
    return local_auth_flow()
```

**Node.js** — same idea, in your server's auth module:
```javascript
function getToken() {
  return process.env.MY_SERVICE_ACCESS_TOKEN || localAuthFlow();
}
```

The env var name (`MY_SERVICE_ACCESS_TOKEN` above) is whatever you set as `access_token_env_var` in the `ServiceConfig` (step 2 below). It just needs to match.

If your service doesn't need per-user OAuth (just API keys), no code changes are needed — the framework injects API keys as env vars directly.

### 2. Create the Lambda handler

Create a directory under `infra/lambda/services/<your-service>/` with two files.

**`handler.py`** — declares *what* to wrap and *how* to authenticate:

```python
from mcp_wrapper import McpServiceHandler, OAuthProviderConfig, ServiceConfig

config = ServiceConfig(
    service_name="my-service",
    mcp_module="my_service_mcp.server",       # python -m my_service_mcp.server
    passthrough_env_vars=["API_BASE_URL"],     # Category 1: non-secret config
    service_secret_name="{prefix}-my-service-service-secrets",  # Category 2
    oauth=OAuthProviderConfig(                 # Category 3: per-user OAuth
        provider_name="my-provider",
        auth_endpoint="https://provider.com/oauth2/authorize",
        token_endpoint="https://provider.com/oauth2/token",
        scopes=["read", "write"],
        # client_id and client_secret are both keys inside the service secret.
        # The framework looks in service secrets first, then Lambda env vars.
        client_id_env="MY_CLIENT_ID",
        client_secret_key="MY_CLIENT_SECRET",
        uses_pkce=True,
    ),
    access_token_env_var="MY_SERVICE_ACCESS_TOKEN",
)

_handler = McpServiceHandler(config)

def handler(event, context):
    return _handler.handle(event, context)
```

In this example, the service secret JSON would contain both the client_id and client_secret:
```json
{"MY_CLIENT_ID": "app-id-123", "MY_CLIENT_SECRET": "secret-456"}
```

`passthrough_env_vars` is for non-secret configuration like API base URLs or region overrides that you set via CDK context or Lambda env vars. Credentials belong in the service secret.

For a **non-Python MCP server**, use `command` and `args` instead of `mcp_module`:

```python
config = ServiceConfig(
    service_name="my-node-service",
    command="/var/task/node_modules/.bin/my-mcp-server",
    args=["--stdio"],
    service_secret_name="{prefix}-my-node-service-secrets",
    oauth=OAuthProviderConfig(...),
    access_token_env_var="MY_SERVICE_ACCESS_TOKEN",
)
```

For a service **without OAuth**, omit the `oauth` and `access_token_env_var` fields:

```python
config = ServiceConfig(
    service_name="my-search",
    mcp_module="my_search_mcp.server",
    service_secret_name="{prefix}-my-search-secrets",
    # API keys live in the service secret; no OAuth needed
)
```

**`requirements.txt`** — tells the bundler what to install into the Lambda package. The framework deps are always needed; your MCP server is listed as a dependency:

```
# Framework dependencies (always required)
mcp
run-mcp-servers-with-aws-lambda
boto3
httpx

# Your MCP server package — can be any pip-installable source:
my-service-mcp                                                     # from PyPI
# my-service-mcp @ git+https://github.com/you/my-service-mcp.git  # from a Git repo
# my-service-mcp @ file:///path/to/local/checkout                  # from a local path

# Note: mcp-wrapper-runtime is installed automatically by the bundler —
# you don't need to list it here.
```

For local development, you can create a `requirements.local.txt` (gitignored) in the same directory with `file://` paths to local checkouts. The bundler uses it when present, falls back to `requirements.txt` otherwise.

For **non-Python MCP servers**, you still need the framework deps in `requirements.txt` (the Lambda handler itself is Python). Bundle your server binary or Node modules into the Lambda package via the `handler_source_dir` — place them alongside `handler.py` and the bundler copies them in.

The MCP server is **not** part of this repository. It is pulled in as a dependency at build time, the same way any Lambda bundles its dependencies.

### 3. Wire it into the CDK app

Add a `ServiceStack` to `infra/app.py`:

```python
ServiceStack(
    app,
    f"{prefix}-my-service",
    env=env,
    service_name="my-service",
    handler_source_dir=os.path.join(_LAMBDA_SERVICES, "my-service"),
    discovery_url=shared.discovery_url,
    oauth_callback_url=shared.oauth_callback_url,
    oauth_state_table_arn=shared.oauth_state_table.table_arn,
    oauth_state_table_name=shared.oauth_state_table.table_name,
    lambda_timeout=120,
    lambda_memory=512,
    lambda_environment={
        "MY_CLIENT_ID": app.node.try_get_context("my_client_id") or "",
        "SERVICE_SECRET_NAME": f"{prefix}-my-service-service-secrets",
    },
)
```

### 4. Deploy

```bash
make deploy-shared                             # first time only (creates Cognito, DCR, OAuth callback)
make deploy-service SERVICE=my-service         # deploy your service
```

### 5. Post-deploy setup

These steps can happen in any order, but must be done before first use:

**Create the service secret** (if your service has Category 2 secrets like client secrets or API keys):

```bash
aws secretsmanager create-secret \
  --name mcp-wrappers-my-service-service-secrets \
  --secret-string '{"MY_CLIENT_SECRET": "the-secret-value"}'
```

**Register the OAuth callback URL** (if your service uses OAuth): take the `OAuthCallbackUrl` from the shared stack output and add it as a redirect URI in your OAuth provider's app registration.

See [Secrets and security](#secrets-and-security) for details on how secrets are stored and scoped.

That's it. The framework handles Cognito, DCR, AgentCore Gateway, OAuth token lifecycle, and Lambda packaging.

## Deployment

```bash
# Install Python dependencies
uv sync

# Bootstrap CDK in your AWS account (first time only)
make bootstrap

# Deploy shared infrastructure (Cognito, DCR, OAuth callback)
make deploy-shared

# Deploy a specific service
make deploy-service SERVICE=msgraph

# Deploy all services
make deploy-all

# Verify endpoints are responding
make verify
```

The first `make` invocation automatically installs the CDK CLI as a local npm dependency — you don't need to install it globally. CDK context parameters are set in `cdk.json` or passed via environment variables (see [CDK context parameters](#cdk-context-parameters)).

### Available targets

| Target | Description |
|--------|-------------|
| `make synth` | Synthesize all CloudFormation templates |
| `make list` | List all stacks |
| `make bootstrap` | Bootstrap CDK in your AWS account (first time) |
| `make deploy-shared` | Deploy shared infrastructure |
| `make deploy-service SERVICE=x` | Deploy a specific service |
| `make deploy-msgraph` | Shortcut for the bundled msgraph example |
| `make deploy-all` | Deploy shared + all services |
| `make verify` | Run post-deploy smoke tests |

## Secrets and security

### How secrets are stored

The framework uses [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) — a managed AWS service that's available in every account with no setup required. You don't provision or deploy anything; you just store and retrieve values via the AWS CLI or SDK.

There are two kinds of secrets per service:

| Type | Secret name pattern | Who creates it |
|------|---------------------|----------------|
| **Service secrets** | `{prefix}-{service}-service-secrets` | You, via `aws secretsmanager create-secret` (once per service) |
| **User credentials** | `{prefix}-{service}-user-{cognito_sub}` | Framework, automatically when a user completes the OAuth flow |

**Service secrets** are a single Secrets Manager entry containing a JSON object. Each key in the JSON becomes a separate environment variable in the subprocess. This is how you pass multiple secrets to a service without creating multiple Secrets Manager entries:

```bash
aws secretsmanager create-secret \
  --name mcp-wrappers-my-service-service-secrets \
  --secret-string '{
    "CLIENT_SECRET": "abc123",
    "API_KEY": "xyz789",
    "WEBHOOK_SECRET": "def456"
  }'
```

The subprocess will see `CLIENT_SECRET=abc123`, `API_KEY=xyz789`, and `WEBHOOK_SECRET=def456` in its environment.

**User credentials** are created automatically by the framework when a user completes the OAuth flow. Each user gets their own secret containing `access_token`, `refresh_token`, and `expires_at`. You never create these manually.

Both types of secrets can be created at any time — before or after deploying the stacks. The Lambda only reads them at invocation time, not deploy time.

### IAM scoping — what each Lambda can access

Each service Lambda's IAM role is scoped to only its own secrets. The policy restricts access to `{prefix}-{service}-*`:

```
# The msgraph Lambda can access:
  mcp-wrappers-msgraph-service-secrets       ✓
  mcp-wrappers-msgraph-user-abc123           ✓

# It CANNOT access:
  mcp-wrappers-gcal-service-secrets          ✗  (different service)
  my-database-password                       ✗  (no prefix match)
  production-api-key                         ✗  (no prefix match)
```

Services are isolated from each other. The msgraph Lambda cannot read Google Calendar's secrets, and neither can read anything else in your account. The same scoping applies to the OAuth callback Lambda (restricted to `{prefix}-*`).

The Lambda also has `CreateSecret` permission within its scope — this is needed because new user credential secrets are created on the fly when a user completes OAuth for the first time (you don't know Cognito user IDs in advance).

### Deployment ordering

The stacks and secrets have this dependency chain:

```
Deploy shared stack ──► Get OAuthCallbackUrl ──► Register URL with OAuth provider
        │                                           (before first OAuth flow)
        │
        └──► Deploy service stack ──► Service is live
                                        │
Create service secret ──────────────────┘ (before first tool invocation)
```

In practice: deploy both stacks first (`make deploy-all`), then handle the two manual steps (create secret + register callback URL) before first use. CDK resolves the inter-stack dependency automatically.

## How the OAuth flow works

When a user first interacts with a service that requires OAuth:

```
1. Agent calls a tool (e.g., list_messages)
   └─ Handler finds no credentials for this user
   └─ Builds OAuth authorization URL (PKCE + state stored in DynamoDB)
   └─ Injects OAUTH_AUTH_URL env var, launches subprocess
   └─ MCP server returns "not authenticated, call start_auth"

2. Agent calls start_auth
   └─ MCP server reads OAUTH_AUTH_URL from env, returns it
   └─ Agent presents URL to user: "Click here to authenticate"

3. User opens URL in browser
   └─ Provider login (Microsoft, Google, etc.)
   └─ Provider redirects to /oauth/callback
   └─ Callback Lambda:
      a. Validates state from DynamoDB
      b. Exchanges code for tokens (standard OAuth2)
      c. Stores tokens in Secrets Manager under {prefix}-{service}-user-{cognito_sub}
      d. Renders "Authentication successful" HTML page

4. Agent calls a tool again (new Lambda invocation)
   └─ Handler loads token from Secrets Manager
   └─ Refreshes if expired (standard OAuth2 refresh grant)
   └─ Injects access token as env var
   └─ MCP server tools work normally
```

Token refresh is transparent — the handler checks expiry on every invocation and refreshes automatically using the stored refresh token.

## Bundled example: Microsoft Graph (msgraph)

The `infra/lambda/services/msgraph/` directory wraps an MCP server for Outlook mail and calendar (msgraph-email-calendar-mcp). It demonstrates the full pattern:

### Deploy

```bash
# 1. Deploy infrastructure
make deploy-all

# 2. Store your Azure app's client secret (anytime before first use)
aws secretsmanager create-secret \
  --name mcp-wrappers-msgraph-service-secrets \
  --secret-string '{"MICROSOFT_CLIENT_SECRET": "your-azure-client-secret"}'

# 3. Register the OAuthCallbackUrl (from deploy output) in your Azure App Registration
#    under Authentication > Web > Redirect URIs
```

### Handler

The complete handler — everything else is framework-managed:

```python
# infra/lambda/services/msgraph/handler.py
from mcp_wrapper import McpServiceHandler, OAuthProviderConfig, ServiceConfig

config = ServiceConfig(
    service_name="msgraph",
    mcp_module="msgraph_mcp.server",
    passthrough_env_vars=["MICROSOFT_CLIENT_ID", "MICROSOFT_TENANT_ID"],
    service_secret_name="{prefix}-msgraph-service-secrets",
    oauth=OAuthProviderConfig(
        provider_name="microsoft",
        auth_endpoint="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        scopes=["User.Read", "Mail.ReadWrite", "Calendars.Read"],
        client_id_env="MICROSOFT_CLIENT_ID",
        client_secret_key="MICROSOFT_CLIENT_SECRET",
        endpoint_params={"tenant_id": "MICROSOFT_TENANT_ID"},
        uses_pkce=True,
    ),
    access_token_env_var="GRAPH_ACCESS_TOKEN",
)

_handler = McpServiceHandler(config)

def handler(event, context):
    return _handler.handle(event, context)
```

### MCP server package reference

The `requirements.txt` pulls in the MCP server as a pip dependency:

```
mcp
run-mcp-servers-with-aws-lambda
boto3
httpx
msgraph-mcp                                           # from PyPI (or use file:// in requirements.local.txt)
```

For local development, create `requirements.local.txt` (gitignored) with the local path:

```
mcp
run-mcp-servers-with-aws-lambda
boto3
httpx
msgraph-mcp @ file:///path/to/msgraph-email-calendar-mcp
```

`mcp-wrapper-runtime` is installed automatically by the bundler in both cases.

## Using an external Cognito pool

If you already have a Cognito User Pool (e.g., from another project like glidepath), pass its details to `SharedInfraStack` to skip creating a new one:

```python
# infra/app.py
shared = SharedInfraStack(
    app, f"{prefix}-shared", env=env,
    external_user_pool_id="us-east-1_xxxxxx",
    external_user_pool_arn="arn:aws:cognito-idp:us-east-1:123456789:userpool/us-east-1_xxxxxx",
    external_hosted_ui_domain="auth.example.com",
    external_resource_server_identifier="my-prefix",
)
```

The DCR bridge and OAuth callback are still created — only the Cognito pool is reused.

## CDK context parameters

Pass via `-c key=value` on the CDK command line, or set defaults in `cdk.json`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `prefix` | `mcp-wrappers` | Resource name prefix for all stacks |
| `microsoft_client_id` | — | Azure App Registration client ID (msgraph) |
| `microsoft_tenant_id` | `organizations` | Azure AD tenant (msgraph) |
| `domain_name` | — | Custom domain for Cognito hosted UI (optional) |
| `hosted_zone_name` | — | Route53 hosted zone for custom domain (optional) |
| `google_client_id_ssm` | — | SSM parameter for Google social federation (optional) |
| `google_client_secret_ssm` | — | SSM parameter for Google social federation (optional) |

## Framework internals

### `McpServiceHandler` lifecycle (per Lambda invocation)

1. **Health check** — responds to `ping`/`health` events immediately
2. **Extract user ID** — reads Cognito `sub` from AgentCore event JWT claims
3. **Build subprocess env** — loads and merges all three credential categories
4. **Launch subprocess** — via `StdioServerAdapterRequestHandler` + `BedrockAgentCoreGatewayTargetHandler`

### Secrets Manager

See [Secrets and security](#secrets-and-security) for naming conventions, IAM scoping, and deployment ordering.

### CDK construct composition

```
SharedInfraStack
  ├── CognitoPool          (User Pool + resource server + hosted UI domain)
  ├── DcrBridge            (DynamoDB table + DCR Lambda + API Gateway)
  └── OAuthBridge          (DynamoDB table + callback Lambda + /oauth/callback)

ServiceStack (one per wrapped service)
  ├── McpServerLambda      (Lambda + bundler + IAM role)
  └── McpAgentCoreGateway  (CfnGateway + CfnGatewayTarget + gateway role)
```
