"""
E2E conftest for quick-suite-router.

Runs against a deployed QuickSuiteModelRouter CloudFormation stack using real AWS.
All tests skip automatically when the stack is not deployed or credentials are absent.

Required environment:
  AWS_PROFILE=aws           (or other standard AWS credential env vars)

Optional environment:
  QS_E2E_ROUTER_STACK_NAME  CloudFormation stack name (default: QuickSuiteModelRouter)
  QS_E2E_REGION             AWS region (default: us-east-1)

Run:
  AWS_PROFILE=aws QS_E2E_REGION=us-west-2 pytest tests/e2e/ -v -m e2e
"""

import json
import os

import boto3
import pytest
import requests as _requests

STACK_NAME = os.environ.get("QS_E2E_ROUTER_STACK_NAME", "QuickSuiteModelRouter")
REGION = os.environ.get("QS_E2E_REGION", "us-east-1")
_AWS_PROFILE = os.environ.get("AWS_PROFILE")


def _session() -> boto3.Session:
    if _AWS_PROFILE:
        return boto3.Session(profile_name=_AWS_PROFILE, region_name=REGION)
    return boto3.Session(region_name=REGION)


def call(api_session: _requests.Session, api_endpoint: str, tool: str, payload: dict,
         timeout: int = 60) -> _requests.Response:
    """POST to /tools/{tool} and return the raw Response."""
    base = api_endpoint.rstrip("/")
    return api_session.post(f"{base}/tools/{tool}", json=payload, timeout=timeout)


# ---------------------------------------------------------------------------
# Session-scoped AWS client fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def lam():
    from botocore.config import Config
    return _session().client("lambda", config=Config(read_timeout=120, connect_timeout=10))


# ---------------------------------------------------------------------------
# CloudFormation outputs
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def cfn_outputs() -> dict[str, str]:
    """
    Fetch CloudFormation outputs for the deployed QuickSuiteModelRouter stack.
    All E2E tests skip if the stack is not deployed or credentials are absent.
    """
    cfn = _session().client("cloudformation")
    try:
        resp = cfn.describe_stacks(StackName=STACK_NAME)
    except Exception as exc:
        pytest.skip(
            f"Stack '{STACK_NAME}' not found or no AWS credentials "
            f"(set AWS_PROFILE=aws and QS_E2E_ROUTER_STACK_NAME if needed): {exc}"
        )
    stacks = resp.get("Stacks", [])
    if not stacks:
        pytest.skip(f"Stack '{STACK_NAME}' returned no data")
    raw = stacks[0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in raw}


@pytest.fixture(scope="session")
def api_endpoint(cfn_outputs) -> str:
    return cfn_outputs["ApiEndpoint"].rstrip("/")


@pytest.fixture(scope="session")
def cognito_user_pool_id(cfn_outputs) -> str:
    return cfn_outputs["CognitoUserPoolId"]


@pytest.fixture(scope="session")
def cognito_client_id(cfn_outputs) -> str:
    return cfn_outputs["CognitoClientId"]


@pytest.fixture(scope="session")
def cognito_token_url(cfn_outputs) -> str:
    return cfn_outputs["CognitoTokenUrl"]


@pytest.fixture(scope="session")
def spend_table_name(cfn_outputs) -> str:
    return cfn_outputs["SpendTableName"]


@pytest.fixture(scope="session")
def query_spend_fn_name(cfn_outputs) -> str:
    return cfn_outputs["QuerySpendFunctionName"]


@pytest.fixture(scope="session")
def cache_table_name(cfn_outputs) -> "str | None":
    """None when the stack was deployed without enable_cache=true."""
    return cfn_outputs.get("CacheTableName")


# ---------------------------------------------------------------------------
# Cognito OAuth token
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def cognito_client_secret(cognito_user_pool_id, cognito_client_id) -> str:
    """Retrieve the Cognito app client secret via the Cognito API."""
    cognito = _session().client("cognito-idp")
    resp = cognito.describe_user_pool_client(
        UserPoolId=cognito_user_pool_id,
        ClientId=cognito_client_id,
    )
    secret = resp["UserPoolClient"].get("ClientSecret")
    if not secret:
        pytest.skip("Cognito app client has no client secret — cannot obtain access token")
    return secret


@pytest.fixture(scope="session")
def access_token(cognito_token_url, cognito_client_id, cognito_client_secret) -> str:
    """Obtain a Cognito client_credentials access token for the E2E session."""
    resp = _requests.post(
        cognito_token_url,
        data={"grant_type": "client_credentials", "scope": "model-router/invoke"},
        auth=(cognito_client_id, cognito_client_secret),
        timeout=15,
    )
    if resp.status_code != 200:
        pytest.skip(f"Failed to obtain Cognito access token: {resp.status_code} {resp.text}")
    token = resp.json().get("access_token")
    if not token:
        pytest.skip(f"No access_token in Cognito response: {resp.json()}")
    return token


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def api(access_token) -> _requests.Session:
    """requests.Session pre-configured with Bearer token for all E2E tests."""
    session = _requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    })
    return session
