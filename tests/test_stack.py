"""
CDK Stack tests for ModelRouterStack.

Verifies:
- cdk synth produces valid CloudFormation
- DynamoDB cache table present when enable_cache=true (default)
- DynamoDB cache table absent when enable_cache=false
- All expected Lambda functions present
- All expected Secrets Manager secrets present
- Bedrock Guardrail resource present
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from stacks.model_router_stack import ModelRouterStack


@pytest.fixture
def default_stack():
    app = cdk.App()
    stack = ModelRouterStack(app, "TestModelRouterStack")
    return Template.from_stack(stack)


@pytest.fixture
def no_cache_stack():
    app = cdk.App(context={"enable_cache": False})
    stack = ModelRouterStack(app, "TestModelRouterStack")
    return Template.from_stack(stack)


def test_synth_succeeds(default_stack):
    """cdk synth produces valid CloudFormation — no exceptions thrown."""
    # If we got here, synthesis succeeded
    assert default_stack is not None


def test_cache_table_present_by_default(default_stack):
    """DynamoDB table exists when enable_cache is true (default): cache + spend ledger."""
    default_stack.resource_count_is("AWS::DynamoDB::Table", 2)


def test_cache_table_absent_when_disabled(no_cache_stack):
    """DynamoDB table: cache absent when disabled, spend ledger always present."""
    no_cache_stack.resource_count_is("AWS::DynamoDB::Table", 1)


def test_all_provider_lambdas_present(default_stack):
    """Router + 4 providers + query-spend = 6 app Lambdas. CDK log-retention adds one more."""
    # 6 application Lambdas + 1 CDK-managed log retention custom resource Lambda
    default_stack.resource_count_is("AWS::Lambda::Function", 7)


def test_provider_lambda_names(default_stack):
    """Each provider Lambda has the expected function name."""
    for suffix in ["router", "provider-bedrock", "provider-anthropic", "provider-openai", "provider-gemini"]:
        default_stack.has_resource_properties(
            "AWS::Lambda::Function",
            {"FunctionName": Match.string_like_regexp(f".*{suffix}.*")},
        )


def test_all_secrets_present(default_stack):
    """One Secrets Manager secret per external provider."""
    default_stack.resource_count_is("AWS::SecretsManager::Secret", 3)


def test_secrets_named_correctly(default_stack):
    """Secrets follow the quicksuite-model-router/{provider} naming."""
    for provider in ["anthropic", "openai", "gemini"]:
        default_stack.has_resource_properties(
            "AWS::SecretsManager::Secret",
            {"Name": f"quicksuite-model-router/{provider}"},
        )


def test_guardrail_present(default_stack):
    """Bedrock CfnGuardrail resource is created."""
    default_stack.resource_count_is("AWS::Bedrock::Guardrail", 1)


def test_guardrail_has_pii_config(default_stack):
    """Guardrail includes PII entity config (SSN block)."""
    default_stack.has_resource_properties(
        "AWS::Bedrock::Guardrail",
        {
            "SensitiveInformationPolicyConfig": Match.object_like({
                "PiiEntitiesConfig": Match.array_with([
                    Match.object_like({"Type": "US_SOCIAL_SECURITY_NUMBER", "Action": "BLOCK"}),
                ])
            })
        },
    )


def test_api_gateway_present(default_stack):
    """REST API is created for the model router."""
    default_stack.resource_count_is("AWS::ApiGateway::RestApi", 1)


def test_cognito_user_pool_present(default_stack):
    """Cognito User Pool is created for OAuth."""
    default_stack.resource_count_is("AWS::Cognito::UserPool", 1)


def test_cloudwatch_dashboard_present(default_stack):
    """CloudWatch usage dashboard is present."""
    default_stack.resource_count_is("AWS::CloudWatch::Dashboard", 1)
