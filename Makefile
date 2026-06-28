.PHONY: synth deploy-shared deploy-service deploy-all verify list bootstrap gen-tools

PREFIX ?= mcp-wrappers

# CDK CLI is installed as a local npm dependency (see package.json).
# Users never need to invoke it directly — use make targets instead.
CDK = ./node_modules/.bin/cdk

# Container engine for CDK Lambda asset bundling. Prefer docker; otherwise fall
# back to a podman shim that adds --userns=keep-id, so rootless podman (e.g. the
# libkrun machine provider) can write to CDK's bind-mounted, root-owned build
# directory while the build runs as the host UID. See scripts/podman-cdk.sh.
export CDK_DOCKER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo $(CURDIR)/scripts/podman-cdk.sh)

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
