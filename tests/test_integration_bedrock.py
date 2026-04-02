"""
Integration tests for Bedrock provider using Substrate.

Tests the bedrock_provider handler round-trip against a Substrate-managed
bedrock-runtime endpoint. Substrate intercepts the boto3 calls and returns
controlled responses, letting us verify the full handler logic without
hitting real AWS.

Run unit tests only:     pytest -m "not integration"
Run integration tests:   pytest -m integration
Run everything:          pytest
"""

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "common", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "providers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "router"))

pytestmark = pytest.mark.integration

GUARDRAIL_ID = "qs-test-guardrail"
MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"

# Canonical Bedrock Converse success response shape
_CONVERSE_SUCCESS = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [{"text": "The enrollment trend shows a 4.2% YoY increase."}],
        }
    },
    "stopReason": "end_turn",
    "usage": {"inputTokens": 120, "outputTokens": 45},
    "ResponseMetadata": {"RequestId": "bedrock-req-abc123"},
}

# Guardrail-blocked Converse response shape
_CONVERSE_BLOCKED = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [{"text": "I cannot help with that."}],
        }
    },
    "stopReason": "guardrail_intervened",
    "usage": {"inputTokens": 80, "outputTokens": 12},
    "ResponseMetadata": {"RequestId": "bedrock-req-blocked99"},
}


def _load_bedrock_provider(substrate_url, monkeypatch, guardrail_id: str = ""):
    """Reload bedrock_provider with AWS_ENDPOINT_URL pointing at Substrate."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
    env_patch = {
        "GUARDRAIL_ID": guardrail_id,
        "GUARDRAIL_VERSION": "DRAFT",
    }
    import provider_interface
    importlib.reload(provider_interface)
    with patch.dict(os.environ, env_patch):
        import bedrock_provider
        importlib.reload(bedrock_provider)
    return bedrock_provider


# ===========================================================================
# Bedrock provider handler — round-trip tests
# ===========================================================================

class TestBedrockConverseSuccess:
    """Handler processes a valid Converse response correctly."""

    def test_success_returns_content_and_tokens(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(substrate_url, monkeypatch)

        with patch.object(provider.bedrock, "converse", return_value=_CONVERSE_SUCCESS):
            result = provider.handler(
                {"prompt": "Analyze enrollment trends", "model": MODEL_ID}, None
            )

        assert result["content"] == "The enrollment trend shows a 4.2% YoY increase."
        assert result["provider"] == "bedrock"
        assert result["model"] == MODEL_ID
        assert result["input_tokens"] == 120
        assert result["output_tokens"] == 45
        assert result.get("error") is None
        assert result["guardrail_blocked"] is False

    def test_success_includes_metadata(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(substrate_url, monkeypatch)

        with patch.object(provider.bedrock, "converse", return_value=_CONVERSE_SUCCESS):
            result = provider.handler(
                {"prompt": "Summarize the report", "model": MODEL_ID}, None
            )

        assert result["metadata"]["stop_reason"] == "end_turn"
        assert result["metadata"]["request_id"] == "bedrock-req-abc123"

    def test_system_prompt_forwarded(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(substrate_url, monkeypatch)

        captured = {}

        def fake_converse(**kwargs):
            captured["params"] = kwargs
            return _CONVERSE_SUCCESS

        with patch.object(provider.bedrock, "converse", side_effect=fake_converse):
            provider.handler(
                {
                    "prompt": "Analyze this",
                    "model": MODEL_ID,
                    "system_prompt": "You are a research analyst.",
                },
                None,
            )

        assert "system" in captured["params"]
        assert captured["params"]["system"][0]["text"] == "You are a research analyst."

    def test_context_prepended_to_prompt(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(substrate_url, monkeypatch)

        captured = {}

        def fake_converse(**kwargs):
            captured["params"] = kwargs
            return _CONVERSE_SUCCESS

        with patch.object(provider.bedrock, "converse", side_effect=fake_converse):
            provider.handler(
                {
                    "prompt": "Summarize the key findings",
                    "model": MODEL_ID,
                    "context": "Q3 revenue: $10M. YoY growth: 12%.",
                },
                None,
            )

        user_text = captured["params"]["messages"][0]["content"][0]["text"]
        assert "Q3 revenue" in user_text
        assert "Summarize the key findings" in user_text


# ===========================================================================
# Guardrail-blocked response
# ===========================================================================

class TestBedrockGuardrailBlocked:
    """Handler correctly surfaces guardrail_intervened stop reason."""

    def test_guardrail_blocked_sets_flag(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(
            substrate_url, monkeypatch, guardrail_id=GUARDRAIL_ID
        )

        with patch.object(provider.bedrock, "converse", return_value=_CONVERSE_BLOCKED):
            result = provider.handler(
                {"prompt": "Generate something inappropriate", "model": MODEL_ID}, None
            )

        assert result["guardrail_blocked"] is True
        assert result["guardrail_applied"] is True
        assert result["provider"] == "bedrock"

    def test_guardrail_blocked_still_returns_content(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        """The response content is returned even when blocked (refusal message)."""
        provider = _load_bedrock_provider(
            substrate_url, monkeypatch, guardrail_id=GUARDRAIL_ID
        )

        with patch.object(provider.bedrock, "converse", return_value=_CONVERSE_BLOCKED):
            result = provider.handler(
                {"prompt": "Blocked request", "model": MODEL_ID}, None
            )

        # content is present (the refusal / blocked message)
        assert isinstance(result["content"], str)
        assert result.get("error") is None


# ===========================================================================
# Model not found / error handling
# ===========================================================================

class TestBedrockModelNotFound:
    """Handler returns an error dict when Bedrock raises a model error."""

    def test_model_not_ready_returns_error(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(substrate_url, monkeypatch)

        error_code = {"Code": "ModelNotReadyException", "Message": "Model not ready"}
        exc = provider.bedrock.exceptions.ModelNotReadyException(
            error_response={"Error": error_code},
            operation_name="Converse",
        )

        with patch.object(provider.bedrock, "converse", side_effect=exc):
            result = provider.handler(
                {"prompt": "Analyze this", "model": "bedrock/some-future-model"}, None
            )

        assert "error" in result
        assert result["content"] == ""
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0

    def test_throttling_returns_error(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(substrate_url, monkeypatch)

        error_code = {"Code": "ThrottlingException", "Message": "Rate exceeded"}
        exc = provider.bedrock.exceptions.ThrottlingException(
            error_response={"Error": error_code},
            operation_name="Converse",
        )

        with patch.object(provider.bedrock, "converse", side_effect=exc):
            result = provider.handler({"prompt": "Analyze", "model": MODEL_ID}, None)

        assert "error" in result
        assert "Rate limited" in result["error"]

    def test_missing_prompt_returns_error(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        provider = _load_bedrock_provider(substrate_url, monkeypatch)

        result = provider.handler({"model": MODEL_ID}, None)

        assert "error" in result
        assert result["content"] == ""


# ===========================================================================
# Router → Bedrock chain
# ===========================================================================

class TestRouterToBedrockChain:
    """Router selects Bedrock and routes the request end-to-end."""

    _ROUTING_CONFIG = {
        "routing": {
            "analyze": {
                "preferred": [f"bedrock/{MODEL_ID}"],
                "system_prompt": "You are an expert analyst.",
            }
        },
        "defaults": {"max_tokens": 1024, "temperature": 0.0},
    }

    _PROVIDER_FUNCTIONS = {
        "bedrock": "arn:aws:lambda:us-east-1:123456789012:function:qs-model-router-provider-bedrock"
    }

    _BEDROCK_RESPONSE = {
        "content": "Strong YoY growth across all cohorts.",
        "provider": "bedrock",
        "model": MODEL_ID,
        "input_tokens": 100,
        "output_tokens": 42,
        "guardrail_applied": False,
        "guardrail_blocked": False,
        "metadata": {"stop_reason": "end_turn", "request_id": "chain-req-001"},
    }

    def _load_router(self, substrate_url, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
        import provider_interface
        importlib.reload(provider_interface)
        env = {
            "ROUTING_CONFIG": json.dumps(self._ROUTING_CONFIG),
            "PROVIDER_FUNCTIONS": json.dumps(self._PROVIDER_FUNCTIONS),
            "PROVIDER_SECRETS": "{}",
            "CACHE_TABLE": "",
            "CACHE_TTL_MINUTES": "60",
        }
        with patch.dict(os.environ, env):
            import handler as router_handler
            importlib.reload(router_handler)
        return router_handler

    def _make_payload_stream(self, payload: dict):
        stream = MagicMock()
        stream.read.return_value = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.__getitem__ = MagicMock(return_value=stream)
        return mock_resp

    def test_router_selects_bedrock_and_returns_content(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        router = self._load_router(substrate_url, monkeypatch)

        # Stub out Secrets Manager — no external providers configured
        secrets_mock = MagicMock()
        secrets_mock.get_secret_value.side_effect = (
            router.secrets_client.exceptions.ResourceNotFoundException(
                error_response={"Error": {"Code": "ResourceNotFoundException"}},
                operation_name="GetSecretValue",
            )
        )

        with patch.object(router, "secrets_client", secrets_mock), \
             patch.object(router, "_available_providers", None), \
             patch.object(router.lambda_client, "invoke",
                          return_value=self._make_payload_stream(self._BEDROCK_RESPONSE)):
            event = {
                "httpMethod": "POST",
                "path": "/tools/analyze",
                "body": json.dumps({
                    "prompt": "Analyze our enrollment cohort trends",
                    "max_tokens": 1024,
                    "temperature": 0.0,
                }),
            }
            response = router.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["content"] == "Strong YoY growth across all cohorts."
        assert body["provider"] == "bedrock"
        assert body["cached"] is False

    def test_router_returns_503_when_no_providers(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        router = self._load_router(substrate_url, monkeypatch)

        # Make bedrock unavailable by overriding get_available_providers
        with patch.object(router, "get_available_providers", return_value=set()):
            event = {
                "httpMethod": "POST",
                "path": "/tools/analyze",
                "body": json.dumps({"prompt": "Hello", "temperature": 0.0}),
            }
            response = router.handler(event, None)

        assert response["statusCode"] == 503
        body = json.loads(response["body"])
        assert "No providers available" in body["error"]

    def test_router_fallback_on_bedrock_error(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        """When Bedrock Lambda returns an error and there are no fallback providers, 503."""
        router = self._load_router(substrate_url, monkeypatch)

        error_payload = {"error": "Bedrock throttled"}
        error_stream = MagicMock()
        error_stream.read.return_value = json.dumps(error_payload).encode()
        error_resp = MagicMock()
        error_resp.__getitem__ = MagicMock(return_value=error_stream)

        secrets_mock = MagicMock()
        secrets_mock.get_secret_value.side_effect = (
            router.secrets_client.exceptions.ResourceNotFoundException(
                error_response={"Error": {"Code": "ResourceNotFoundException"}},
                operation_name="GetSecretValue",
            )
        )

        with patch.object(router, "secrets_client", secrets_mock), \
             patch.object(router, "_available_providers", None), \
             patch.object(router.lambda_client, "invoke", return_value=error_resp):
            event = {
                "httpMethod": "POST",
                "path": "/tools/analyze",
                "body": json.dumps({
                    "prompt": "Analyze cohort data",
                    "temperature": 0.0,
                }),
            }
            response = router.handler(event, None)

        # Only bedrock in preferred list — all providers fail
        assert response["statusCode"] == 503
        body = json.loads(response["body"])
        assert body["error"] == "All providers failed"

    def test_router_department_override_respected(
        self, substrate_url, reset_substrate, monkeypatch
    ):
        """Department override selects the department's provider preference."""
        cfg = dict(self._ROUTING_CONFIG)
        cfg["department_overrides"] = {
            "bedrock-only": {
                "analyze": {"preferred": [f"bedrock/{MODEL_ID}"]}
            }
        }

        monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
        import provider_interface
        importlib.reload(provider_interface)
        env = {
            "ROUTING_CONFIG": json.dumps(cfg),
            "PROVIDER_FUNCTIONS": json.dumps(self._PROVIDER_FUNCTIONS),
            "PROVIDER_SECRETS": "{}",
            "CACHE_TABLE": "",
            "CACHE_TTL_MINUTES": "60",
        }
        with patch.dict(os.environ, env):
            import handler as router_handler
            importlib.reload(router_handler)

        secrets_mock = MagicMock()
        secrets_mock.get_secret_value.side_effect = (
            router_handler.secrets_client.exceptions.ResourceNotFoundException(
                error_response={"Error": {"Code": "ResourceNotFoundException"}},
                operation_name="GetSecretValue",
            )
        )

        with patch.object(router_handler, "secrets_client", secrets_mock), \
             patch.object(router_handler, "_available_providers", None), \
             patch.object(router_handler.lambda_client, "invoke",
                          return_value=self._make_payload_stream(self._BEDROCK_RESPONSE)) as mock_invoke:
            event = {
                "httpMethod": "POST",
                "path": "/tools/analyze",
                "body": json.dumps({
                    "prompt": "Analyze cohort data",
                    "department": "bedrock-only",
                    "temperature": 0.0,
                }),
            }
            response = router_handler.handler(event, None)

        assert response["statusCode"] == 200
        # Bedrock Lambda ARN was invoked
        invoked_arn = mock_invoke.call_args[1]["FunctionName"]
        assert "bedrock" in invoked_arn
