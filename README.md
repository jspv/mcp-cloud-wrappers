# MCP Cloud Wrappers

**Take any stdio MCP server and deploy it to the cloud — accessible from ChatGPT, Claude.ai, and any MCP-compatible client on web and mobile.**

Most MCP servers run locally — they work great with Claude Desktop or Claude Code on your machine, but they can't be used from ChatGPT, Claude.ai on the web, or mobile apps. This framework changes that. You bring an existing stdio MCP server, and this framework deploys it as a cloud-hosted MCP endpoint that any client can connect to, with full user authentication and per-user OAuth for external services.

## What this does

- **Any stdio MCP server** → cloud-hosted MCP endpoint with a URL
- **Works with ChatGPT, Claude.ai, and any MCP client** — web, mobile, desktop
- **Per-user authentication** — each user logs in via Cognito and connects their own external accounts (Microsoft, Google, etc.)
- **Automatic OAuth management** — token exchange, refresh, per-user storage in Secrets Manager
- **Zero MCP server changes required** for basic services; one-line change for services with per-user OAuth
- **Add a new service in minutes** — create a directory with 3-4 config files, deploy

## How it works

The framework wraps MCP servers that you develop and maintain **outside this repository**. Each MCP server is any program that speaks the MCP stdio protocol — Python packages (built with [FastMCP](https://github.com/jlowin/fastmcp), the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), etc.), Node.js servers, Go binaries, or anything else that reads/writes MCP JSON-RPC over stdin/stdout. The framework:

1. Bundles your MCP server into an AWS Lambda deployment package
2. Exposes it as a cloud MCP endpoint via [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html)
3. Handles caller authentication (Cognito + Dynamic Client Registration)
4. Manages per-user OAuth tokens for external services (Microsoft, Google, etc.)
5. Provides an auth setup web page where users connect their external accounts

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
| 1. Service config | `TENANT_ID`, `API_BASE_URL` | `service.env` file in the service directory | You, committed to git |
| 2. Service secrets | `CLIENT_ID`, `CLIENT_SECRET`, `API_KEY` | Secrets Manager (one JSON object per service — each key becomes an env var) | You, created once via AWS CLI |
| 3. Per-user credentials | Access token | Secrets Manager (one per user per service) | Framework, via OAuth flow |

Category 1 is for non-secret configuration — it lives in `service.env` alongside `handler.py`. All credentials — including client IDs — belong in Category 2 (the service secret in Secrets Manager).

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js (CDK CLI runs on Node; the rest of the project is Python)
- AWS CLI configured with credentials
- CDK bootstrapped: `make bootstrap` (or see [Deployment](#deployment))

## Repository structure

```
mcp-cloud-wrappers/
├── packages/
│   └── mcp-wrapper-runtime/             # Framework runtime (installed into each Lambda)
│       └── src/mcp_wrapper/
│           ├── config.py                # ServiceConfig, OAuthProviderConfig, load_oauth_json
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
│           └── <your-service>/              # One directory per wrapped MCP service
│               ├── handler.py               #   config: what to wrap, how to authenticate
│               ├── service.env              #   non-secret config (committed)
│               ├── tools.json               #   tool definitions (generated via gen-tools)
│               ├── requirements.txt.example #   dependency template (committed)
│               ├── requirements.txt         #   (gitignored) actual deps with your paths
│               └── service.local.env        #   (gitignored) local config overrides
│
├── scripts/
│   ├── gen_tools.py                     # Generate tools.json from MCP server
│   └── verify_deployment.py             # Post-deploy smoke test
├── cdk.json
├── Makefile
└── pyproject.toml
```

## Wrapping a new MCP service — step by step

This section walks through the full process. The bundled `msgraph` wrapper is a concrete example of every step described here.

### 1. Prepare your MCP server

Your MCP server lives in its own repository, package registry, or wherever you keep it. It can be written in **any language** — the framework launches it as a subprocess and communicates over stdio.

There are two things to adapt in your server for Lambda compatibility:

#### a. Accept a framework-injected access token

If your service uses per-user OAuth, find the place in your MCP server that acquires an access token and add an env var check at the top. This lets the same code work both locally (where it runs its own auth) and inside Lambda (where the framework manages auth):

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

#### b. Don't write to the Lambda filesystem

AWS Lambda's `/var/task` directory is **read-only**. If your MCP server writes files at startup (token caches, SQLite databases, temp files), those writes will fail in Lambda.

Guard any filesystem writes so they're skipped when the framework is managing auth:

```python
import os

def _framework_managed():
    """True when running inside the MCP Lambda wrapper framework."""
    return bool(os.environ.get("MY_SERVICE_ACCESS_TOKEN") or os.environ.get("OAUTH_AUTH_URL"))

# Before any file write:
if not _framework_managed():
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_token_cache(...)
```

Common sources of filesystem writes to watch for:
- **Token caches** (MSAL, google-auth, etc.) — skip cache reads/writes when framework-managed
- **Session state files** — the framework handles state via DynamoDB/Secrets Manager
- **SQLite databases** — if unavoidable, write to `/tmp` (the only writable path in Lambda)

### 2. Create the service directory

Create a directory under `infra/lambda/services/<your-service>/` with these files:

```
infra/lambda/services/my-service/
├── handler.py              # what to wrap, how to authenticate
├── service.env             # non-secret config (committed)
├── oauth.json              # OAuth provider config (if service uses OAuth)
├── tools.json              # tool definitions (generated via gen-tools)
├── requirements.txt.example
├── requirements.txt        # (gitignored)
└── service.local.env       # (gitignored)
```

**`service.env`** — non-secret configuration for this service. These values are baked into the Lambda package at deploy time, so **edit this before deploying**:

```
# service.env
MY_TENANT_ID=my-org-123
MY_API_BASE_URL=https://api.provider.com/v1
```

For local development, create `service.local.env` (gitignored) to override values without changing the committed file. The framework loads `service.local.env` first when present.

**`handler.py`** — declares *what* to wrap and *how* to authenticate:

```python
from mcp_wrapper import McpServiceHandler, ServiceConfig, load_oauth_json

config = ServiceConfig(
    service_name="my-service",
    mcp_module="my_service_mcp.server",       # python -m my_service_mcp.server
    passthrough_env_vars=["MY_TENANT_ID"],     # from service.env
    service_secret_name="{prefix}-my-service-service-secrets",
    oauth=load_oauth_json(),                   # reads oauth.json (single source of truth)
    access_token_env_var="MY_SERVICE_ACCESS_TOKEN",
)

_handler = McpServiceHandler(config)

def handler(event, context):
    return _handler.handle(event, context)
```

`passthrough_env_vars` names which values from `service.env` to forward to the subprocess. Credentials belong in the service secret (see step 3). OAuth provider config (endpoints, scopes, client keys) is defined in `oauth.json` — not in this file.

For a **non-Python MCP server**, use `command` and `args` instead of `mcp_module`:

```python
config = ServiceConfig(
    service_name="my-node-service",
    command="/var/task/node_modules/.bin/my-mcp-server",
    args=["--stdio"],
    service_secret_name="{prefix}-my-node-service-secrets",
    oauth=load_oauth_json(),
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

**`requirements.txt.example`** (committed) — a template showing the dependencies. Copy it to `requirements.txt` (gitignored) and fill in your package source:

```
# Copy this file to requirements.txt and update paths.
# requirements.txt is gitignored (contains local paths).

# Framework dependencies (always required)
mcp
run-mcp-servers-with-aws-lambda
boto3
httpx

# MCP server package — uncomment ONE of these:
# my-service-mcp                                                     # from PyPI
# my-service-mcp @ git+https://github.com/you/my-service-mcp.git    # from Git
# my-service-mcp @ file:///path/to/local/checkout                    # from local path
```

`mcp-wrapper-runtime` is installed automatically by the bundler — you don't need to list it.

For **non-Python MCP servers**, you still need the framework deps in `requirements.txt` (the Lambda handler itself is Python). Bundle your server binary or Node modules into the Lambda package via the `handler_source_dir` — place them alongside `handler.py` and the bundler copies them in.

The MCP server is **not** part of this repository. It is pulled in as a dependency at build time, the same way any Lambda bundles its dependencies.

That's it — no other files to edit. The CDK app auto-discovers every directory under `infra/lambda/services/` that contains a `handler.py` and creates a stack for it.

### 3. Pre-deploy setup

These must be done **before deploying** the service:

**Edit `service.env`** with your service's non-secret config. These values are baked into the Lambda package at deploy time:

```
# infra/lambda/services/my-service/service.env
LAMBDA_TIMEOUT=120
LAMBDA_MEMORY=512
MY_TENANT_ID=your-actual-tenant-id
```

`LAMBDA_TIMEOUT` and `LAMBDA_MEMORY` are framework settings (used by CDK at deploy time). Everything else is passed to the MCP subprocess.

**Create the service secret** in Secrets Manager (credentials like client IDs and API keys):

```bash
aws secretsmanager create-secret \
  --name mcp-wrappers-my-service-service-secrets \
  --secret-string '{"MY_CLIENT_ID": "your-client-id", "MY_CLIENT_SECRET": "your-secret"}'
```

**Generate `tools.json`** — AgentCore needs to know which tools the MCP server exposes. This introspects the MCP server and writes `tools.json` to the service directory:

```bash
make gen-tools SERVICE=my-service
```

The script finds the MCP server's project directory automatically from the `file://` path in `requirements.local.txt`. Re-run this whenever the MCP server's tool definitions change.

For non-Python MCP servers (or if there's no local checkout), create `tools.json` manually.

### 4. Deploy

```bash
make deploy-shared                             # first time only (creates Cognito, DCR, OAuth callback)
make deploy-service SERVICE=my-service         # deploy your service
```

### 5. Post-deploy setup

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

# Create a Cognito user (needed to authenticate with the gateway)
aws cognito-idp admin-create-user \
  --user-pool-id $(aws cloudformation describe-stacks --stack-name mcp-wrappers-shared \
    --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId622CD4B2`].OutputValue' --output text) \
  --username your-email@example.com \
  --user-attributes Name=email,Value=your-email@example.com Name=email_verified,Value=true \
  --temporary-password 'TempPass123!'

# Deploy a specific service
make deploy-service SERVICE=my-service

# Deploy all services
make deploy-all

# Verify endpoints are responding
make verify
```

The first `make` invocation automatically installs the CDK CLI as a local npm dependency — you don't need to install it globally.

**macOS users**: Lambda requires Linux ARM64 binaries for compiled Python packages (pydantic, cryptography, etc.). The bundler automatically falls back to building inside a container. Set `CDK_DOCKER=podman` if you use Podman instead of Docker:

```bash
CDK_DOCKER=podman make deploy-service SERVICE=my-service
```

Or export it in your shell profile so you don't have to pass it every time.

The Cognito user only needs to be created once. On first login via the hosted UI, you'll be prompted to set a permanent password.

### Available targets

| Target | Description |
|--------|-------------|
| `make synth` | Synthesize all CloudFormation templates |
| `make list` | List all stacks |
| `make bootstrap` | Bootstrap CDK in your AWS account (first time) |
| `make gen-tools SERVICE=x` | Generate tools.json from the MCP server |
| `make deploy-shared` | Deploy shared infrastructure |
| `make deploy-service SERVICE=x` | Deploy a specific service |
| `make deploy-all` | Deploy shared + all discovered services |
| `make verify` | Run post-deploy smoke tests |
| `make auth` | Open auth setup page in browser |

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
# The service-a Lambda can access:
  mcp-wrappers-service-a-service-secrets     ✓
  mcp-wrappers-service-a-user-abc123         ✓

# It CANNOT access:
  mcp-wrappers-service-b-service-secrets     ✗  (different service)
  my-database-password                       ✗  (no prefix match)
  production-api-key                         ✗  (no prefix match)
```

Services are isolated from each other. Each Lambda can only read secrets matching its own `{prefix}-{service}-*` pattern. The same scoping applies to the OAuth callback Lambda (restricted to `{prefix}-*`).

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

In practice: set up `service.env`, service secrets, and `tools.json` first (step 3), then deploy (step 4), then register the OAuth callback URL (step 5). CDK resolves inter-stack dependencies automatically.

## How the OAuth flow works

When a user first interacts with a service that requires OAuth:

```
1. Agent calls a tool
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

## Connecting external services

After deploying, users need to connect their external accounts (Microsoft, Google, etc.)
before the MCP tools can access their data.

### Auth setup page

Run:

```bash
make auth
```

This opens a web page where you:
1. Log in with your Cognito account (same credentials as the MCP gateway)
2. See all available services and their connection status
3. Click "Connect" to authenticate with each external service

The page handles the full OAuth flow — you just click through the provider's login.

### For chat users (Claude.ai, ChatGPT, etc.)

When you first use a tool that needs authentication, the agent will call `start_auth`
which returns the auth setup page URL. Open it in your browser, complete the login,
then tell the agent to try again.

### Auth setup URL

The URL is shown in the stack outputs after deploy:

```bash
aws cloudformation describe-stacks --stack-name mcp-wrappers-shared \
  --query 'Stacks[0].Outputs[?contains(OutputKey,`AuthSetupUrl`)].OutputValue' \
  --output text
```

Share this URL with end users who need to connect their accounts.

## Bundled example: Microsoft Graph (msgraph)

The `infra/lambda/services/msgraph/` directory wraps an MCP server for Outlook mail and calendar (msgraph-email-calendar-mcp). It demonstrates the full pattern:

### Setup and deploy

```bash
# 1. Edit service.env with your tenant ID
#    infra/lambda/services/msgraph/service.env:
#    MICROSOFT_TENANT_ID=your-tenant-id

# 2. Create the service secret with your Azure app credentials
aws secretsmanager create-secret \
  --name mcp-wrappers-msgraph-service-secrets \
  --secret-string '{"MICROSOFT_CLIENT_ID": "your-azure-client-id"}'

# 3. Generate tools.json from the MCP server
make gen-tools SERVICE=msgraph

# 4. Deploy
make deploy-all

# 5. Register the OAuthCallbackUrl (from deploy output) in your Azure App Registration
#    under Authentication > Web > Redirect URIs
```

### Handler

The complete handler — everything else is framework-managed:

```python
# infra/lambda/services/msgraph/handler.py
from mcp_wrapper import McpServiceHandler, ServiceConfig, load_oauth_json

config = ServiceConfig(
    service_name="msgraph",
    mcp_module="msgraph_mcp.server",
    passthrough_env_vars=["MICROSOFT_TENANT_ID"],
    service_secret_name="{prefix}-msgraph-service-secrets",
    oauth=load_oauth_json(),        # reads oauth.json — single source of truth
    access_token_env_var="GRAPH_ACCESS_TOKEN",
)

_handler = McpServiceHandler(config)

def handler(event, context):
    return _handler.handle(event, context)
```

### MCP server package reference

Copy `requirements.txt.example` to `requirements.txt` (gitignored) and set the path to your local checkout:

```
mcp
run-mcp-servers-with-aws-lambda
boto3
httpx
msgraph-mcp @ file:///path/to/msgraph-email-calendar-mcp
```

`mcp-wrapper-runtime` is installed automatically by the bundler.

## Using an external Cognito pool

If you already have a Cognito User Pool (e.g., from another project), you can reuse it instead of creating a new one. This requires editing `infra/app.py` — the only case where that file needs modification:

```python
# infra/app.py — pass external pool details to SharedInfraStack
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
| `domain_name` | — | Custom domain for Cognito hosted UI (optional) |
| `hosted_zone_name` | — | Route53 hosted zone for custom domain (optional) |
| `google_client_id_ssm` | — | SSM parameter for Google social federation (optional) |
| `google_client_secret_ssm` | — | SSM parameter for Google social federation (optional) |

Per-service configuration does not use CDK context. Non-secret config goes in `service.env`, credentials go in Secrets Manager. See [Three categories of environment variables](#three-categories-of-environment-variables).

## Framework internals

### End-to-end request flow (detailed)

This is the complete path of a single tool call, from the MCP client through to the MCP server and back. Understanding this flow is important for debugging.

```
MCP Client (Claude.ai, ChatGPT, etc.)
  │
  │  MCP JSON-RPC over HTTPS
  ▼
AgentCore Gateway
  │  Validates Cognito JWT (CUSTOM_JWT authorizer)
  │  Rejects if invalid — tool call never reaches Lambda
  │
  │  Invokes request interceptor Lambda
  ▼
Interceptor Lambda (infra/lambda/interceptor/handler.py)
  │  Receives: {interceptorInputVersion, mcp: {gatewayRequest: {headers, body}}}
  │  Decodes JWT from Authorization header (base64, no verification — already validated)
  │  Extracts Cognito "sub" claim
  │  Injects _cognito_sub into body.params.arguments
  │  Returns: {interceptorOutputVersion: "1.0", mcp: {transformedGatewayRequest: {body}}}
  │
  │  AgentCore forwards the modified request to the target Lambda
  ▼
MCP Server Lambda — handler entry point (handler.py in service directory)
  │  Receives: event = tool arguments dict (e.g. {"folder": "inbox", "_cognito_sub": "abc123"})
  │  context.client_context.custom = {bedrockAgentCoreToolName: "target___tool_name"}
  │
  │  Calls McpServiceHandler.handle(event, context)
  ▼
McpServiceHandler.handle() (packages/mcp-wrapper-runtime/src/mcp_wrapper/handler.py)
  │
  │  1. Health check: if event has "ping"/"health", return status immediately
  │
  │  2. Extract user ID: reads event.get("_cognito_sub")
  │     Returns the Cognito sub or None
  │
  │  3. Strip _cognito_sub from event: event.pop("_cognito_sub", None)
  │     FastMCP/Pydantic would reject it as an unexpected tool argument
  │
  │  4. Build subprocess environment (three categories):
  │     Category 1: passthrough env vars from service.env (e.g. TENANT_ID)
  │     Category 2: service secrets from Secrets Manager (e.g. CLIENT_ID)
  │     Category 3: per-user OAuth credentials:
  │       - If user_id is available AND credentials exist in Secrets Manager:
  │           Load tokens, refresh if expired, set access_token_env_var + OAUTH_AUTHENTICATED=true
  │       - If user_id is available but NO credentials:
  │           Set OAUTH_AUTHENTICATED=false, OAUTH_AUTH_URL=AUTH_SETUP_URL
  │       - If user_id is None (interceptor not working):
  │           Set OAUTH_AUTHENTICATED=false (no OAUTH_AUTH_URL — can't do per-user lookup)
  │
  │  5. Launch MCP subprocess: python -m {mcp_module}
  │     Subprocess receives the merged env vars
  │     StdioServerAdapterRequestHandler bridges JSON-RPC to stdio
  │     BedrockAgentCoreGatewayTargetHandler handles the AgentCore protocol
  ▼
MCP Server subprocess (e.g. msgraph_mcp.server)
  │  Receives tool call via stdio (JSON-RPC)
  │  Reads GRAPH_ACCESS_TOKEN (or equivalent) from env — uses it for API calls
  │  If not authenticated: reads OAUTH_AUTH_URL from env, returns it via start_auth tool
  │  Executes tool, returns result via stdio
  ▼
Response flows back: subprocess → handler → AgentCore Gateway → MCP Client
```

### Auth setup page flow (detailed)

When a user needs to connect an external service (one-time setup):

```
User visits /auth/setup (via make auth, start_auth URL, or direct link)
  │
  ▼
Auth Setup Lambda — /auth/setup route
  │  No session → redirects to Cognito hosted UI login
  ▼
Cognito Hosted UI
  │  User logs in (email/password, possibly MFA)
  │  If already logged in (browser cookie), may auto-complete
  │  Redirects to /auth/callback?code=xxx&state=yyy
  ▼
Auth Setup Lambda — /auth/callback route
  │  Validates state from DynamoDB (prevents CSRF)
  │  Exchanges Cognito auth code for tokens (POST to Cognito token endpoint)
  │  Decodes ID token → extracts sub and email
  │  Creates DynamoDB session (10-minute TTL, keyed by random session token)
  │  Renders service connection page HTML
  │  Each service card shows: display_name, connected/not connected, [Connect] button
  │  Connect button URL: /auth/connect/{service}?session={token}
  ▼
User clicks [Connect]
  │
  ▼
Auth Setup Lambda — /auth/connect/{service} route
  │  Validates session token from DynamoDB → gets Cognito sub
  │  Loads service OAuth config from SERVICE_OAUTH_CONFIGS env var
  │  Reads client_id from Secrets Manager (service secret)
  │  Generates PKCE code_verifier + code_challenge
  │  Stores OAuth state in DynamoDB: {state, user_id, service_name, token_endpoint,
  │    client_id, code_verifier, return_url=/auth/setup?session=xxx, ttl}
  │  Redirects to external provider's OAuth authorization URL
  ▼
External Provider (Microsoft, Google, etc.)
  │  User logs in and approves permissions
  │  Redirects to /oauth/callback?code=xxx&state=yyy
  ▼
OAuth Callback Lambda — /oauth/callback route (existing, shared)
  │  Validates state from DynamoDB
  │  Exchanges authorization code for tokens (POST to provider's token endpoint)
  │  With PKCE code_verifier if present
  │  Stores tokens in Secrets Manager: {prefix}-{service}-user-{cognito_sub}
  │  Checks for return_url in state record
  │  If return_url present: redirects to /auth/setup?session=xxx&connected={service}
  │  If no return_url: shows static "Authentication successful" HTML
  ▼
Auth Setup Lambda — /auth/setup route (return visit)
  │  Session token present → loads session from DynamoDB
  │  connected={service} param → shows success flash message
  │  Re-checks connection status for all services
  │  User sees "{display_name} — Connected ✓"
```

### Identity propagation — why inject-then-strip

AgentCore Gateway validates Cognito JWTs but does **not** forward claims to Lambda targets. The Lambda receives only tool arguments in `event` and metadata in `context.client_context.custom` (tool name, gateway ID — no user identity).

The framework uses a **request interceptor** to work around this:

1. **Interceptor** decodes the JWT from the Authorization header and injects `_cognito_sub` into the JSON-RPC `params.arguments`
2. **AgentCore** passes the modified arguments as `event` to the target Lambda
3. **McpServiceHandler** reads `event.get("_cognito_sub")` to identify the user
4. **McpServiceHandler** removes `_cognito_sub` from `event` before forwarding to `BedrockAgentCoreGatewayTargetHandler`

The removal is necessary because FastMCP validates tool arguments against the function signature via Pydantic. An unexpected `_cognito_sub` parameter would cause a validation error. The identity rides in the arguments briefly, gets extracted by the handler, then gets stripped before reaching the MCP subprocess.

The interceptor response **must** include `"interceptorOutputVersion": "1.0"` at the top level — AgentCore silently drops the request without it.

### Secrets Manager

See [Secrets and security](#secrets-and-security) for naming conventions, IAM scoping, and deployment ordering.

### 30-tool limit

AgentCore Gateway paginates `tools/list` MCP responses at 30 tools per page and returns a `nextCursor` for additional pages. However, current MCP clients (Claude.ai, ChatGPT) do not follow pagination — they take the first page only. This means **only the first 30 tools (sorted alphabetically) are visible to clients.**

The `gen-tools` script enforces this limit and will error if the MCP server exposes more than 30 tools. If your service needs more, reduce the tool count in the MCP server (consolidate tools, remove rarely-used ones) or split across multiple gateway targets.

AgentCore also supports `searchType: SEMANTIC` which replaces the tool list with a single search tool for natural language discovery. However, current MCP clients don't use it — they expect tools to appear directly in `tools/list`.

### Diagnostic logging

**TODO: Remove before production.** The interceptor, handler, and credential manager currently emit `[interceptor]` and `[mcp-wrapper]` log lines to stderr (CloudWatch) for debugging the identity propagation and credential loading flow. These should be removed or gated behind a `LOG_LEVEL` env var once field testing is complete. Files with diagnostic logging:
- `infra/lambda/interceptor/handler.py`
- `packages/mcp-wrapper-runtime/src/mcp_wrapper/handler.py`
- `packages/mcp-wrapper-runtime/src/mcp_wrapper/credentials.py`

### CDK construct composition

```
SharedInfraStack
  ├── CognitoPool          (User Pool + resource server + hosted UI domain)
  ├── DcrBridge            (DynamoDB table + DCR Lambda + API Gateway)
  ├── OAuthBridge          (DynamoDB table + callback Lambda + /oauth/callback)
  └── AuthSetup            (Cognito client + auth setup Lambda + /auth/* routes)

ServiceStack (one per wrapped service)
  ├── McpServerLambda      (Lambda + bundler + IAM role)
  └── McpAgentCoreGateway  (CfnGateway + CfnGatewayTarget + interceptor Lambda + gateway role)
```
