.PHONY: synth deploy-shared deploy-service deploy-all verify list bootstrap gen-tools

PREFIX ?= mcp-wrappers

# CDK CLI is installed as a local npm dependency (see package.json).
# Users never need to invoke it directly — use make targets instead.
CDK = ./node_modules/.bin/cdk

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

verify:
	uv run python scripts/verify_deployment.py
