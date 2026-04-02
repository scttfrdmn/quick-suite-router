"""
Shared fixtures for Quick Suite Model Router tests.
"""

import json
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

# Set fake AWS credentials before any boto3 import so the credential
# provider chain doesn't try to hit real endpoints or load plugins.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Make the Lambda layers and handlers importable
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO_ROOT, "lambdas", "common", "python"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambdas", "router"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambdas", "providers"))
sys.path.insert(0, os.path.join(REPO_ROOT, "stacks"))


# ---------------------------------------------------------------------------
# Routing config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def routing_config():
    return {
        "routing": {
            "analyze": {
                "preferred": [
                    "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                    "anthropic/claude-sonnet-4-20250514",
                    "openai/gpt-4o",
                    "gemini/gemini-2.5-pro",
                ],
                "system_prompt": "You are an expert analyst.",
            },
            "summarize": {
                "preferred": [
                    "bedrock/amazon.nova-pro-v1:0",
                    "openai/gpt-4o-mini",
                ],
                "system_prompt": "You are a concise summarizer.",
            },
            "code": {
                "preferred": [
                    "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                    "anthropic/claude-sonnet-4-20250514",
                ],
                "system_prompt": "You are an expert software engineer.",
            },
        },
        "defaults": {"max_tokens": 4096, "temperature": 0.7},
    }


@pytest.fixture
def provider_functions():
    return {
        "bedrock": "arn:aws:lambda:us-east-1:123456789012:function:qs-model-router-provider-bedrock",
        "anthropic": "arn:aws:lambda:us-east-1:123456789012:function:qs-model-router-provider-anthropic",
        "openai": "arn:aws:lambda:us-east-1:123456789012:function:qs-model-router-provider-openai",
        "gemini": "arn:aws:lambda:us-east-1:123456789012:function:qs-model-router-provider-gemini",
    }


@pytest.fixture
def provider_secrets():
    return {
        "anthropic": "arn:aws:secretsmanager:us-east-1:123456789012:secret:quicksuite-model-router/anthropic",
        "openai": "arn:aws:secretsmanager:us-east-1:123456789012:secret:quicksuite-model-router/openai",
        "gemini": "arn:aws:secretsmanager:us-east-1:123456789012:secret:quicksuite-model-router/gemini",
    }


# ---------------------------------------------------------------------------
# Provider response fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def success_response_bedrock():
    return {
        "content": "This is a test analysis response.",
        "provider": "bedrock",
        "model": "anthropic.claude-sonnet-4-20250514-v1:0",
        "input_tokens": 100,
        "output_tokens": 50,
        "guardrail_applied": True,
        "guardrail_blocked": False,
        "metadata": {"stop_reason": "end_turn", "request_id": "req-123"},
    }


@pytest.fixture
def success_response_anthropic():
    return {
        "content": "This is a test analysis response.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "input_tokens": 100,
        "output_tokens": 50,
        "guardrail_applied": True,
        "guardrail_blocked": False,
        "metadata": {"stop_reason": "end_turn", "id": "msg-123"},
    }


@pytest.fixture
def error_response():
    return {
        "content": "",
        "provider": "bedrock",
        "model": "",
        "error": "Rate limited",
        "input_tokens": 0,
        "output_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Mock Lambda invoke response builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Substrate integration fixture
# ---------------------------------------------------------------------------

_SUBSTRATE_BIN = os.path.expanduser("~/src/substrate/bin/substrate")


@pytest.fixture(scope="session")
def substrate_url():
    """
    Yields the Substrate endpoint URL for integration tests.

    Uses SUBSTRATE_ENDPOINT env var if set (assumes already running).
    Otherwise starts the binary from ~/src/substrate/bin/substrate.
    Skips if the binary is not found.
    """
    import requests  # noqa: PLC0415

    url = os.environ.get("SUBSTRATE_ENDPOINT", "http://localhost:4566")

    # Already running?
    try:
        requests.get(f"{url}/health", timeout=1)
        yield url
        return
    except Exception:
        pass

    if not os.path.exists(_SUBSTRATE_BIN):
        pytest.skip("Substrate binary not found; build ~/src/substrate or set SUBSTRATE_ENDPOINT")
        return

    proc = subprocess.Popen(
        [_SUBSTRATE_BIN, "server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            requests.get(f"{url}/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.terminate()
        pytest.skip("Substrate did not become healthy in time")
        return

    yield url

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def reset_substrate(substrate_url):
    """Reset all Substrate state before and after the test."""
    import requests  # noqa: PLC0415

    requests.post(f"{substrate_url}/v1/state/reset")
    yield
    requests.post(f"{substrate_url}/v1/state/reset")


# ---------------------------------------------------------------------------
# Lambda response helper (kept here for use in test_router.py)
# ---------------------------------------------------------------------------


def make_lambda_response(payload: dict):
    """Build a mock Lambda invoke response with a streaming Payload."""
    import io
    mock_resp = MagicMock()
    mock_resp.__getitem__ = lambda self, key: (
        io.BytesIO(json.dumps(payload).encode()) if key == "Payload" else None
    )
    mock_resp.get = lambda key, default=None: (
        io.BytesIO(json.dumps(payload).encode()) if key == "Payload" else default
    )
    # Support resp["Payload"].read() pattern
    payload_stream = MagicMock()
    payload_stream.read.return_value = json.dumps(payload).encode()
    mock_resp.__getitem__ = MagicMock(return_value=payload_stream)
    return mock_resp
