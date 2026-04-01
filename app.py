#!/usr/bin/env python3
"""Quick Suite Model Router — CDK Application Entry Point."""
import aws_cdk as cdk
from stacks.model_router_stack import ModelRouterStack

app = cdk.App()

ModelRouterStack(
    app,
    "QuickSuiteModelRouter",
    description=(
        "Multi-provider LLM router for Amazon Quick Suite "
        "via Bedrock AgentCore Gateway"
    ),
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "us-east-1",
    ),
)

app.synth()
