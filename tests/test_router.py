"""
Tests for the Router Lambda (lambdas/router/handler.py).

Covers:
- select_provider(): availability sets, explicit overrides
- handle_tool_invocation(): successful routing
- Fallback chain when first provider fails
- Cache hit / miss / skip behavior
- Status endpoint
- Error cases: missing prompt, no providers
"""

import sys
import os

# Fake credentials must be set before any boto3 import
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import json
import importlib
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "common", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "router"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lambda_payload(data: dict):
    payload_mock = MagicMock()
    payload_mock.read.return_value = json.dumps(data).encode()
    response_mock = MagicMock()
    response_mock.__getitem__ = MagicMock(side_effect=lambda k: payload_mock if k == "Payload" else None)
    return response_mock


def _load_handler(routing_config, provider_functions, provider_secrets, cache_table=""):
    """Import handler with specific env vars."""
    env = {
        "ROUTING_CONFIG": json.dumps(routing_config),
        "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
        "PROVIDER_SECRETS": json.dumps(provider_secrets),
        "CACHE_TABLE": cache_table,
        "CACHE_TTL_MINUTES": "60",
    }
    with patch.dict(os.environ, env):
        import handler
        importlib.reload(handler)
        return handler


# ---------------------------------------------------------------------------
# select_provider
# ---------------------------------------------------------------------------

class TestSelectProvider:
    def test_prefers_first_available(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "anthropic", "openai"}):
            provider, model = h.select_provider("analyze")
        assert provider == "bedrock"
        assert "claude-sonnet" in model

    def test_skips_unavailable_falls_to_second(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"anthropic", "openai"}):
            provider, model = h.select_provider("analyze")
        assert provider == "anthropic"

    def test_explicit_override_selects_requested_provider(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "openai"}):
            provider, model = h.select_provider("analyze", explicit="openai")
        assert provider == "openai"

    def test_explicit_override_ignored_if_unavailable(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}):
            provider, model = h.select_provider("analyze", explicit="openai")
        # Falls through to bedrock since openai is unavailable
        assert provider == "bedrock"

    def test_no_available_providers_returns_none(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", set()):
            provider, model = h.select_provider("analyze")
        assert provider is None
        assert model is None

    def test_unknown_tool_falls_back_to_analyze_config(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}):
            provider, model = h.select_provider("unknown_tool")
        assert provider == "bedrock"


# ---------------------------------------------------------------------------
# handle_tool_invocation
# ---------------------------------------------------------------------------

class TestHandleToolInvocation:
    def test_successful_invocation_returns_200(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {
            "content": "Analysis result",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100,
            "output_tokens": 50,
            "guardrail_applied": True,
            "guardrail_blocked": False,
        }

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Analyze this data"})}
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["content"] == "Analysis result"
        assert body["provider"] == "bedrock"

    def test_missing_prompt_returns_400(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        event = {"tool": "analyze", "body": json.dumps({})}
        result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        assert "No prompt" in json.loads(result["body"])["error"]

    def test_no_providers_returns_503(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", set()):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello"})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 503

    def test_tool_extracted_from_path(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {"content": "ok", "provider": "bedrock", "model": "m",
                   "input_tokens": 0, "output_tokens": 0, "guardrail_applied": False, "guardrail_blocked": False}
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"path": "/tools/summarize", "body": json.dumps({"prompt": "Summarize this"})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

class TestFallbackChain:
    def test_fallback_to_second_provider_on_error(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)

        error_payload = {"errorMessage": "Bedrock throttled", "errorType": "ThrottlingException"}
        success_payload = {
            "content": "Fallback response",
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 80,
            "output_tokens": 40,
            "guardrail_applied": True,
            "guardrail_blocked": False,
        }

        invoke_side_effects = [
            _make_lambda_payload(error_payload),
            _make_lambda_payload(success_payload),
        ]

        with patch.object(h, "_available_providers", {"bedrock", "anthropic"}), \
             patch.object(h.lambda_client, "invoke", side_effect=invoke_side_effects):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Analyze this"})}
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["provider"] == "anthropic"
        assert "_fallback" in body

    def test_all_providers_fail_returns_503(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        error_payload = {"errorMessage": "Failed", "errorType": "Exception"}

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(error_payload)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Test"})}
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 503


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------

class TestCacheBehavior:
    def test_cache_hit_returns_cached_response(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets, cache_table="qs-model-router-cache")
        cached = {
            "content": "Cached response",
            "provider": "bedrock",
            "model": "m",
            "input_tokens": 0,
            "output_tokens": 0,
        }

        # handler imports 'from provider_interface import cache_get' — patch the bound reference
        with patch.object(h, "cache_get", return_value=cached), \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h, "_available_providers", {"bedrock"}):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 0.0})}
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["cached"] is True
        assert body["content"] == "Cached response"

    def test_cache_miss_stores_response(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets, cache_table="qs-model-router-cache")
        success = {"content": "Fresh", "provider": "bedrock", "model": "m",
                   "input_tokens": 10, "output_tokens": 5,
                   "guardrail_applied": False, "guardrail_blocked": False}

        with patch.object(h, "cache_get", return_value=None), \
             patch.object(h, "cache_put") as mock_put, \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 0.0})}
            h.handle_tool_invocation(event)

        mock_put.assert_called_once()

    def test_cache_skipped_for_high_temperature(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets, cache_table="qs-model-router-cache")
        success = {"content": "Hot response", "provider": "bedrock", "model": "m",
                   "input_tokens": 10, "output_tokens": 5,
                   "guardrail_applied": False, "guardrail_blocked": False}

        with patch.object(h, "cache_get") as mock_get, \
             patch.object(h, "cache_put") as mock_put, \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 0.9})}
            h.handle_tool_invocation(event)

        mock_get.assert_not_called()
        mock_put.assert_not_called()

    def test_cache_skipped_when_skip_cache_true(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets, cache_table="qs-model-router-cache")
        success = {"content": "Skip cache response", "provider": "bedrock", "model": "m",
                   "input_tokens": 10, "output_tokens": 5,
                   "guardrail_applied": False, "guardrail_blocked": False}

        with patch.object(h, "cache_get") as mock_get, \
             patch.object(h, "cache_put") as mock_put, \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({
                "prompt": "Hello", "temperature": 0.0, "skip_cache": True
            })}
            h.handle_tool_invocation(event)

        mock_get.assert_not_called()
        mock_put.assert_not_called()


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    def test_status_includes_all_providers(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "anthropic"}):
            result = h.handle_status()
        body = json.loads(result["body"])
        assert body["providers"]["bedrock"]["available"] is True
        assert body["providers"]["anthropic"]["available"] is True
        assert body["providers"]["openai"]["available"] is False
        assert "analyze" in body["tools"]

    def test_status_reflects_tools_in_config(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}):
            result = h.handle_status()
        body = json.loads(result["body"])
        assert set(body["tools"]) == {"analyze", "summarize", "code"}
