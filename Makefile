.PHONY: synth deploy-shared deploy-service deploy-all verify list bootstrap gen-tools

PREFIX ?= mcp-wrappers

# CDK CLI is installed as a local npm dependency (see package.json).
# Users never need to invoke it directly — use make targets instead.
CDK = ./node_modules/.bin/cdk

# Use podman if docker is not available.
export CDK_DOCKER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)

node_modules/.bin/cdk:
	npm install --save-dev aws-cdk

synth: node_modules/.bin/cdk
	$(CDK) synth

list: node_modules/.bin/cdk
	$(CDK) ls

bootstrap: node_modules/.bin/cdk
	$(CDK) bootstrap

deploy-shared: node_modules/.bin/cdk
	$(CDK) deploy $(PREFIX)-shared

deploy-service: node_modules/.bin/cdk
	$(CDK) deploy $(PREFIX)-$(SERVICE)

deploy-all: node_modules/.bin/cdk
	$(CDK) deploy --all

gen-tools:
	uv run python scripts/gen_tools.py $(SERVICE)

auth:
	uv run python scripts/open_auth_page.py

verify:
	uv run python scripts/verify_deployment.py
