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

import os
import sys

# Fake credentials must be set before any boto3 import
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import importlib
import json
import time
from unittest.mock import MagicMock, patch

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
            provider, model, _ = h.select_provider("analyze")
        assert provider == "bedrock"
        assert "claude-sonnet" in model

    def test_skips_unavailable_falls_to_second(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"anthropic", "openai"}):
            provider, model, _ = h.select_provider("analyze")
        assert provider == "anthropic"

    def test_explicit_override_selects_requested_provider(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "openai"}):
            provider, model, _ = h.select_provider("analyze", explicit="openai")
        assert provider == "openai"

    def test_explicit_override_ignored_if_unavailable(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}):
            provider, model, _ = h.select_provider("analyze", explicit="openai")
        # Falls through to bedrock since openai is unavailable
        assert provider == "bedrock"

    def test_no_available_providers_returns_none(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", set()):
            provider, model, _ = h.select_provider("analyze")
        assert provider is None
        assert model is None

    def test_unknown_tool_falls_back_to_analyze_config(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}):
            provider, model, _ = h.select_provider("unknown_tool")
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

    def test_cache_key_differs_by_tool(self):
        """Same prompt and model on different tools must produce different cache keys."""
        import provider_interface
        key_analyze = provider_interface.cache_key("hello", "model-x", tool="analyze")
        key_generate = provider_interface.cache_key("hello", "model-x", tool="generate")
        key_summarize = provider_interface.cache_key("hello", "model-x", tool="summarize")
        assert key_analyze != key_generate
        assert key_analyze != key_summarize
        assert key_generate != key_summarize

    def test_cache_key_same_tool_is_stable(self):
        """Same inputs always produce the same key."""
        import provider_interface
        k1 = provider_interface.cache_key("hello", "model-x", tool="analyze")
        k2 = provider_interface.cache_key("hello", "model-x", tool="analyze")
        assert k1 == k2


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


# ---------------------------------------------------------------------------
# TestHandleToolInvocation — validation edge cases
# ---------------------------------------------------------------------------

class TestToolInvocationValidation:
    def test_temperature_above_max_returns_400(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 2.5})}
        result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        assert "temperature" in json.loads(result["body"])["error"]

    def test_max_tokens_above_limit_returns_400(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "max_tokens": 50000})}
        result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        assert "max_tokens" in json.loads(result["body"])["error"]

    def test_temperature_at_boundary_zero_accepted(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {"content": "ok", "provider": "bedrock", "model": "m",
                   "input_tokens": 0, "output_tokens": 0,
                   "guardrail_applied": False, "guardrail_blocked": False}
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 0.0})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200

    def test_temperature_at_boundary_one_accepted(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {"content": "ok", "provider": "bedrock", "model": "m",
                   "input_tokens": 0, "output_tokens": 0,
                   "guardrail_applied": False, "guardrail_blocked": False}
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 1.0})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200

    def test_temperature_below_zero_returns_400(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": -0.1})}
        result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        assert "temperature" in json.loads(result["body"])["error"]

    def test_max_tokens_at_minimum_accepted(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {"content": "ok", "provider": "bedrock", "model": "m",
                   "input_tokens": 0, "output_tokens": 0,
                   "guardrail_applied": False, "guardrail_blocked": False}
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "max_tokens": 1})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200

    def test_max_tokens_at_maximum_accepted(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {"content": "ok", "provider": "bedrock", "model": "m",
                   "input_tokens": 0, "output_tokens": 0,
                   "guardrail_applied": False, "guardrail_blocked": False}
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "max_tokens": 16384})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200

    def test_prompt_too_large_returns_400(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        big_prompt = "x" * 100_001
        event = {"tool": "analyze", "body": json.dumps({"prompt": big_prompt})}
        result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        assert "Prompt" in json.loads(result["body"])["error"]


# ---------------------------------------------------------------------------
# Department overrides
# ---------------------------------------------------------------------------

class TestDepartmentOverrides:
    def test_department_override_selects_department_preferred_provider(self, provider_functions, provider_secrets):
        config = {
            "routing": {
                "analyze": {
                    "preferred": [
                        "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                        "openai/gpt-4o",
                    ],
                    "system_prompt": "You are an analyst.",
                },
            },
            "department_overrides": {
                "openai-only": {
                    "analyze": {"preferred": ["openai/gpt-4o"]},
                },
            },
            "defaults": {"max_tokens": 4096, "temperature": 0.7},
        }
        h = _load_handler(config, provider_functions, provider_secrets)
        result = h._preferred_for("analyze", "openai-only")
        assert result == ["openai/gpt-4o"]

    def test_unknown_department_falls_back_to_default(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        result = h._preferred_for("analyze", "unknown-dept")
        assert len(result) > 0
        assert "bedrock" in result[0]

    def test_unknown_department_logs_warning(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h.logger, "warning") as mock_warn:
            h._preferred_for("analyze", "mystery-dept")
        assert mock_warn.called
        logged = json.loads(mock_warn.call_args[0][0])
        assert logged.get("unrecognized_department") == "mystery-dept"

    def test_department_override_missing_tool_falls_back(self, provider_functions, provider_secrets):
        config = {
            "routing": {
                "analyze": {
                    "preferred": ["bedrock/claude-sonnet"],
                    "system_prompt": "...",
                },
            },
            "department_overrides": {
                "finance": {"summarize": {"preferred": ["openai/gpt-4o-mini"]}},
            },
            "defaults": {"max_tokens": 4096, "temperature": 0.7},
        }
        h = _load_handler(config, provider_functions, provider_secrets)
        # "analyze" not overridden for "finance" → default routing
        result = h._preferred_for("analyze", "finance")
        assert "bedrock/claude-sonnet" in result[0]


# ---------------------------------------------------------------------------
# Provider availability cache
# ---------------------------------------------------------------------------

class TestGetAvailableProviders:
    def test_cache_returns_stale_when_ttl_not_expired(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        h._available_providers = {"bedrock", "openai"}
        h._available_providers_fetched_at = time.time()
        with patch.object(h.secrets_client, "get_secret_value") as mock_get:
            result = h.get_available_providers()
        mock_get.assert_not_called()
        assert "openai" in result

    def test_cache_refreshes_after_ttl(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        h._available_providers = {"bedrock", "openai"}
        h._available_providers_fetched_at = time.time() - 400  # beyond 300s TTL
        with patch.object(h.secrets_client, "get_secret_value") as mock_get:
            mock_get.return_value = {"SecretString": json.dumps({"api_key": "sk-test"})}
            h.get_available_providers()
        mock_get.assert_called()

    def test_stale_cache_preserved_on_error(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        h._available_providers = {"bedrock", "anthropic"}
        h._available_providers_fetched_at = time.time() - 400  # stale
        with patch.object(h.secrets_client, "get_secret_value") as mock_get:
            mock_get.side_effect = Exception("Secrets Manager unavailable")
            result = h.get_available_providers()
        # Stale cache preserved — not cleared
        assert "anthropic" in result


# ---------------------------------------------------------------------------
# PHI-tagged request routing (Issue #9)
# ---------------------------------------------------------------------------

class TestPhiRouting:
    """When data_classification == "phi", only Bedrock providers are candidates."""

    def test_phi_request_selects_bedrock_when_available(self, routing_config, provider_functions, provider_secrets):
        """PHI-tagged request routes to Bedrock even when Anthropic is preferred first in config."""
        # Routing config with Anthropic first
        config = {
            "routing": {
                "analyze": {
                    "preferred": [
                        "anthropic/claude-sonnet-4-20250514",
                        "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                    ],
                    "system_prompt": "You are an analyst.",
                },
            },
            "defaults": {"max_tokens": 4096, "temperature": 0.7},
        }
        h = _load_handler(config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "anthropic"}):
            provider, model, _ = h.select_provider("analyze", phi_mode=True)
        assert provider == "bedrock"

    def test_phi_request_never_selects_anthropic(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "anthropic", "openai", "gemini"}):
            provider, model, _ = h.select_provider("analyze", phi_mode=True)
        assert provider == "bedrock"

    def test_phi_request_never_selects_openai(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        # Only openai and gemini available — no Bedrock
        with patch.object(h, "_available_providers", {"openai", "gemini"}):
            provider, model, _ = h.select_provider("analyze", phi_mode=True)
        assert provider is None

    def test_phi_request_no_bedrock_configured_returns_none(self, provider_functions, provider_secrets):
        """If the preferred list has no Bedrock entries, phi_mode yields (None, None)."""
        config = {
            "routing": {
                "analyze": {
                    "preferred": ["anthropic/claude-sonnet-4-20250514", "openai/gpt-4o"],
                    "system_prompt": "You are an analyst.",
                },
            },
            "defaults": {"max_tokens": 4096, "temperature": 0.7},
        }
        h = _load_handler(config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"anthropic", "openai"}):
            provider, model, _ = h.select_provider("analyze", phi_mode=True)
        assert provider is None
        assert model is None

    def test_phi_explicit_non_bedrock_ignored_silently(self, routing_config, provider_functions, provider_secrets):
        """Explicit provider override for a non-Bedrock provider is silently ignored in PHI mode."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "openai"}):
            # Caller explicitly asks for openai but data_classification=phi
            provider, model, _ = h.select_provider("analyze", explicit="openai", phi_mode=True)
        assert provider == "bedrock"

    def test_non_phi_request_unaffected(self, routing_config, provider_functions, provider_secrets):
        """Non-PHI requests still use the full preference list."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"anthropic", "openai"}):
            provider, model, _ = h.select_provider("analyze", phi_mode=False)
        assert provider == "anthropic"

    def test_phi_field_in_invocation_triggers_bedrock_routing(self, routing_config, provider_functions, provider_secrets):
        """End-to-end: data_classification=phi in the request body routes to Bedrock."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {
            "content": "PHI-safe response",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100,
            "output_tokens": 50,
            "guardrail_applied": True,
            "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock", "anthropic", "openai"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({
                "prompt": "Analyze this patient record",
                "data_classification": "phi",
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["provider"] == "bedrock"

    def test_phi_field_case_insensitive(self, routing_config, provider_functions, provider_secrets):
        """data_classification='PHI' (uppercase) also triggers PHI mode."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock", "anthropic"}):
            provider, _, _skip = h.select_provider("analyze")  # non-phi for baseline
        # Now test with uppercase PHI via full invocation
        success = {
            "content": "ok",
            "provider": "bedrock",
            "model": "m",
            "input_tokens": 0,
            "output_tokens": 0,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }
        h2 = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h2, "_available_providers", {"bedrock", "anthropic"}), \
             patch.object(h2.lambda_client, "invoke", return_value=_make_lambda_payload(success)):
            event = {"tool": "analyze", "body": json.dumps({
                "prompt": "test",
                "data_classification": "PHI",
            })}
            result = h2.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["provider"] == "bedrock"

    def test_phi_no_bedrock_returns_503(self, routing_config, provider_functions, provider_secrets):
        """When PHI mode is active and no Bedrock is available, return 503."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"anthropic", "openai"}):
            event = {"tool": "analyze", "body": json.dumps({
                "prompt": "test",
                "data_classification": "phi",
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 503


# ---------------------------------------------------------------------------
# CORS header (#43)
# ---------------------------------------------------------------------------

class TestCorsHeader:
    def test_cors_wildcard_by_default(self, routing_config, provider_functions, provider_secrets):
        """Without CORS_ALLOWED_ORIGIN env var, header defaults to '*'."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        result = h.handle_status()
        assert result["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_cors_header_uses_env_var(self, routing_config, provider_functions, provider_secrets):
        """CORS_ALLOWED_ORIGIN env var overrides the wildcard."""
        env = {
            "ROUTING_CONFIG": json.dumps(routing_config),
            "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
            "PROVIDER_SECRETS": json.dumps(provider_secrets),
            "CORS_ALLOWED_ORIGIN": "https://quicksuite.example.edu",
        }
        with patch.dict(os.environ, env):
            import handler
            importlib.reload(handler)
            h = handler
        result = h.handle_status()
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://quicksuite.example.edu"

    def test_cors_header_present_on_error_responses(self, routing_config, provider_functions, provider_secrets):
        """CORS header is set on 400 error responses too."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        event = {"tool": "analyze", "body": json.dumps({})}
        result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        assert "Access-Control-Allow-Origin" in result["headers"]


# ---------------------------------------------------------------------------
# Cognito claims auth (#41)
# ---------------------------------------------------------------------------

class TestCognitoClaimsAuth:
    def _event_with_claims(self, prompt, claims, body_overrides=None):
        body = {"prompt": prompt}
        if body_overrides:
            body.update(body_overrides)
        return {
            "tool": "analyze",
            "body": json.dumps(body),
            "requestContext": {
                "authorizer": {"claims": claims},
                "requestId": "req-test",
            },
        }

    def _success_payload(self):
        return {
            "content": "ok",
            "provider": "bedrock",
            "model": "m",
            "input_tokens": 10,
            "output_tokens": 5,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

    def test_claims_sub_used_as_user_id(self, routing_config, provider_functions, provider_secrets):
        """user_id from Cognito sub, not body."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(self._success_payload())), \
             patch.object(h, "spend_record_write") as mock_spend:
            event = self._event_with_claims(
                "test",
                claims={"sub": "cognito-uuid-123", "custom:department": "cs"},
                body_overrides={"user_id": "should-be-ignored"},
            )
            h.handle_tool_invocation(event)
        call_kwargs = mock_spend.call_args[1]
        assert call_kwargs["user_id"] == "cognito-uuid-123"

    def test_claims_department_used_not_body(self, routing_config, provider_functions, provider_secrets):
        """department from Cognito custom:department, not body."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(self._success_payload())), \
             patch.object(h, "spend_record_write") as mock_spend:
            event = self._event_with_claims(
                "test",
                claims={"sub": "user-abc", "custom:department": "biology"},
                body_overrides={"department": "wrong-dept"},
            )
            h.handle_tool_invocation(event)
        call_kwargs = mock_spend.call_args[1]
        assert call_kwargs["department"] == "biology"

    def test_no_claims_falls_back_to_body(self, routing_config, provider_functions, provider_secrets):
        """Without claims, body department and user_id are used."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(self._success_payload())), \
             patch.object(h, "spend_record_write") as mock_spend:
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "test", "department": "physics", "user_id": "alice"}),
            }
            h.handle_tool_invocation(event)
        call_kwargs = mock_spend.call_args[1]
        assert call_kwargs["department"] == "physics"
        assert call_kwargs["user_id"] == "alice"

    def test_empty_claims_dict_falls_back_to_body(self, routing_config, provider_functions, provider_secrets):
        """Empty claims dict (no attributes) treats as no-claims path."""
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(self._success_payload())), \
             patch.object(h, "spend_record_write") as mock_spend:
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "test", "department": "math", "user_id": "bob"}),
                "requestContext": {"authorizer": {"claims": {}}},
            }
            h.handle_tool_invocation(event)
        call_kwargs = mock_spend.call_args[1]
        assert call_kwargs["department"] == "math"


# ---------------------------------------------------------------------------
# Content audit logging (#33)
# ---------------------------------------------------------------------------

class TestContentAuditLogging:
    def _success_payload(self):
        return {
            "content": "The answer is 42",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100,
            "output_tokens": 50,
            "guardrail_applied": True,
            "guardrail_blocked": False,
        }

    def test_audit_log_emitted_when_enabled(self, routing_config, provider_functions, provider_secrets, caplog):
        """When CONTENT_AUDIT_LOGGING=true, a structured audit record is logged."""
        import logging
        env = {
            "ROUTING_CONFIG": json.dumps(routing_config),
            "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
            "PROVIDER_SECRETS": json.dumps(provider_secrets),
            "CONTENT_AUDIT_LOGGING": "true",
        }
        with patch.dict(os.environ, env):
            import handler
            importlib.reload(handler)
            h = handler

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(self._success_payload())), \
             patch.object(h, "spend_record_write"), \
             caplog.at_level(logging.INFO):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "What is 6 * 7?"})}
            h.handle_tool_invocation(event)

        audit_records = [r for r in caplog.records if '"audit_log": "content"' in r.getMessage()]
        assert len(audit_records) >= 1
        record = json.loads(audit_records[0].getMessage())
        assert record["audit_log"] == "content"
        assert "prompt_hash" in record
        assert "response_hash" in record
        assert len(record["prompt_hash"]) == 64  # SHA-256 hex
        assert len(record["response_hash"]) == 64

    def test_raw_prompt_not_in_audit_log(self, routing_config, provider_functions, provider_secrets, caplog):
        """Raw prompt text must NOT appear in any log record."""
        import logging
        env = {
            "ROUTING_CONFIG": json.dumps(routing_config),
            "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
            "PROVIDER_SECRETS": json.dumps(provider_secrets),
            "CONTENT_AUDIT_LOGGING": "true",
        }
        prompt_text = "secret-financial-data-do-not-log-xyz123"
        with patch.dict(os.environ, env):
            import handler
            importlib.reload(handler)
            h = handler

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(self._success_payload())), \
             patch.object(h, "spend_record_write"), \
             caplog.at_level(logging.DEBUG):
            event = {"tool": "analyze", "body": json.dumps({"prompt": prompt_text})}
            h.handle_tool_invocation(event)

        # No log record should contain the raw prompt text (except the initial
        # structured log of the full event at handler entry — exclude that one
        # and check the audit-specific records)
        audit_records = [r for r in caplog.records if '"audit_log": "content"' in r.getMessage()]
        for record in audit_records:
            assert prompt_text not in record.getMessage()

    def test_audit_log_not_emitted_when_disabled(self, routing_config, provider_functions, provider_secrets, caplog):
        """When CONTENT_AUDIT_LOGGING is not set, no audit record is emitted."""
        import logging
        h = _load_handler(routing_config, provider_functions, provider_secrets)

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(self._success_payload())), \
             patch.object(h, "spend_record_write"), \
             caplog.at_level(logging.INFO):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "test"})}
            h.handle_tool_invocation(event)

        audit_records = [r for r in caplog.records if '"audit_log": "content"' in r.getMessage()]
        assert len(audit_records) == 0


# ---------------------------------------------------------------------------
# Capability and context-window routing
# ---------------------------------------------------------------------------

def _caps_routing_config():
    """Routing config with model_capabilities and model_context_windows."""
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
        },
        "defaults": {"max_tokens": 4096, "temperature": 0.7},
        "model_capabilities": {
            "bedrock/anthropic.claude-sonnet-4-20250514-v1:0": ["vision", "long_context", "function_calling", "structured_output"],
            "anthropic/claude-sonnet-4-20250514": ["vision", "long_context", "function_calling", "structured_output"],
            "openai/gpt-4o": ["vision", "function_calling", "structured_output"],
            "gemini/gemini-2.5-pro": ["vision", "long_context", "function_calling", "structured_output"],
            "bedrock/amazon.nova-pro-v1:0": ["vision", "function_calling"],
            "openai/gpt-4o-mini": ["function_calling"],
        },
        "model_context_windows": {
            "bedrock/anthropic.claude-sonnet-4-20250514-v1:0": 200000,
            "anthropic/claude-sonnet-4-20250514": 200000,
            "openai/gpt-4o": 128000,
            "gemini/gemini-2.5-pro": 1000000,
            "bedrock/amazon.nova-pro-v1:0": 300000,
            "openai/gpt-4o-mini": 128000,
        },
    }


class TestCapabilityAndContextRouting:
    def test_capability_match_routes_to_correct_provider(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        # All four providers have "vision"; bedrock is first in preferred list
        with patch.object(h, "_available_providers", {"bedrock", "anthropic", "openai", "gemini"}):
            pk, mid, reason = h.select_provider("analyze", required_capabilities=["vision"])
        assert pk == "bedrock"
        assert reason == ""

    def test_missing_cap_skips_provider(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        # Only gemini has "long_context" in the config; remove bedrock and anthropic from available
        h = _load_handler(cfg, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"openai", "gemini"}):
            # openai/gpt-4o lacks "long_context"; gemini has it
            pk, mid, reason = h.select_provider("analyze", required_capabilities=["long_context"])
        assert pk == "gemini"
        assert reason == ""

    def test_all_caps_missing_returns_unsatisfiable(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"openai"}):
            # openai/gpt-4o lacks "long_context"
            pk, mid, reason = h.select_provider("analyze", required_capabilities=["long_context"])
        assert pk is None
        assert reason == "unsatisfiable_capabilities"

    def test_context_fits_model_a_selects_a(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        # budget of 50000 fits all models
        with patch.object(h, "_available_providers", {"bedrock", "anthropic", "openai"}):
            pk, mid, reason = h.select_provider("analyze", context_budget=50000)
        assert pk == "bedrock"
        assert reason == ""

    def test_context_exceeds_small_model_falls_to_larger(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        # budget of 150000 exceeds openai (128k) but fits bedrock/anthropic (200k) and gemini (1M)
        with patch.object(h, "_available_providers", {"openai", "gemini"}):
            # openai first in preferred would be skipped (128k < 150k); gemini selected
            pk, mid, reason = h.select_provider("analyze", context_budget=150000)
        assert pk == "gemini"
        assert reason == ""

    def test_context_exceeds_all_returns_context_limit_exceeded(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        # budget of 2000000 exceeds all models
        with patch.object(h, "_available_providers", {"bedrock", "anthropic", "openai", "gemini"}):
            pk, mid, reason = h.select_provider("analyze", context_budget=2000000)
        assert pk is None
        assert reason == "context_limit_exceeded"

    def test_invocation_returns_400_context_limit_exceeded(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        # Patch select_provider to return context_limit_exceeded
        with patch.object(h, "select_provider", return_value=(None, None, "context_limit_exceeded")), \
             patch.object(h, "_available_providers", {"bedrock"}):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello"})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["code"] == "context_limit_exceeded"
        assert "tokens_in_estimate" in body

    def test_invocation_returns_400_unsatisfiable_capabilities(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        with patch.object(h, "select_provider", return_value=(None, None, "unsatisfiable_capabilities")), \
             patch.object(h, "_available_providers", {"bedrock"}):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "capabilities": ["hologram"]})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["code"] == "unsatisfiable_capabilities"

    def test_tokens_in_estimate_present_in_successful_response(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        success = {
            "content": "ok", "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 10, "output_tokens": 5,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)), \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h, "spend_record_write"):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 0.5})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "tokens_in_estimate" in body
        assert body["tokens_in_estimate"] > 0

    def test_capabilities_as_string_coerced_to_list(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        success = {
            "content": "ok", "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 10, "output_tokens": 5,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)), \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h, "spend_record_write"):
            # capabilities sent as a single string — should be coerced to ["vision"]
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "capabilities": "vision", "temperature": 0.5})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200

    def test_model_with_no_context_window_never_skipped(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        # Remove model_context_windows entirely — all windows default to 0 (unconfigured)
        del cfg["model_context_windows"]
        h = _load_handler(cfg, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}):
            # Even with a huge budget, model with no window configured is never skipped
            pk, mid, reason = h.select_provider("analyze", context_budget=9999999)
        assert pk == "bedrock"
        assert reason == ""

    def test_fallback_chain_respects_capability_filter(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        # bedrock (first in list) fails; anthropic has "long_context" so it should be selected
        # openai lacks "long_context"
        success_payload = {
            "content": "Fallback result", "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 80, "output_tokens": 40,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock", "anthropic", "openai", "gemini"}), \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success_payload)):
            result = h._fallback(
                "analyze", "bedrock",
                {"model": "anthropic.claude-sonnet-4-20250514-v1:0", "prompt": "Hello"},
                {"error": "throttled"},
                required_capabilities=["long_context"],
            )
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# Dry-run mode (#37)
# ---------------------------------------------------------------------------

class TestDryRunMode:
    def test_dry_run_returns_estimate_without_invoking_model(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke") as mock_invoke:
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Analyze this", "dry_run": True})}
            result = h.handle_tool_invocation(event)
        mock_invoke.assert_not_called()
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["dry_run"] is True
        assert "estimated_cost_usd" in body
        assert isinstance(body["estimated_cost_usd"], float)

    def test_dry_run_response_includes_provider_model_tokens(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke"):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello world", "dry_run": True})}
            result = h.handle_tool_invocation(event)
        body = json.loads(result["body"])
        assert body["selected_provider"] == "bedrock"
        assert "selected_model" in body
        assert body["tokens_in_estimate"] > 0

    def test_dry_run_capability_filter_still_applies(self, provider_functions, provider_secrets):
        cfg = _caps_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        # openai lacks "long_context" — no provider satisfies it
        with patch.object(h, "_available_providers", {"openai"}):
            event = {"tool": "analyze", "body": json.dumps({
                "prompt": "Hello", "dry_run": True, "capabilities": ["long_context"]
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["code"] == "unsatisfiable_capabilities"

    def test_dry_run_no_spend_ledger_write(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke"), \
             patch.object(h, "spend_record_write") as mock_spend:
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "dry_run": True})}
            h.handle_tool_invocation(event)
        mock_spend.assert_not_called()

    def test_dry_run_omitted_executes_normally(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        success = {
            "content": "Normal result", "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 50, "output_tokens": 20,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(success)), \
             patch.object(h, "spend_record_write"):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Hello", "temperature": 0.5})}
            result = h.handle_tool_invocation(event)
        body = json.loads(result["body"])
        assert body.get("dry_run") is None
        assert body["content"] == "Normal result"


# ---------------------------------------------------------------------------
# Per-user rate limiting — Lambda authorizer (#36)
# ---------------------------------------------------------------------------

class TestPerUserRateLimitingAuthorizer:
    def _import_authorizer(self):
        import importlib.util
        authorizer_path = os.path.join(
            os.path.dirname(__file__), "..", "lambdas", "authorizer", "handler.py"
        )
        spec = importlib.util.spec_from_file_location("authorizer_handler", authorizer_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _make_jwt(self, payload: dict) -> str:
        """Build a minimally-valid JWT structure (unsigned) for testing."""
        import base64 as _b64
        header = _b64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
        body_part = _b64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        return f"{header}.{body_part}.fakesig"

    def test_authorizer_context_includes_usage_identifier_key(self):
        mod = self._import_authorizer()
        claims = {"sub": "user-abc-123", "email": "test@example.edu"}
        token = self._make_jwt(claims)
        event = {
            "authorizationToken": f"Bearer {token}",
            "methodArn": "arn:aws:execute-api:us-east-1:123456789012:abcdef/prod/POST/tools/analyze",
        }
        result = mod.handler(event, None)
        assert result["usageIdentifierKey"] == "user-abc-123"
        assert result["context"]["sub"] == "user-abc-123"

    def test_authorizer_missing_token_raises_unauthorized(self):
        import pytest as _pytest
        mod = self._import_authorizer()
        event = {"methodArn": "arn:aws:execute-api:us-east-1:123456:api/prod/POST/tools/analyze"}
        with _pytest.raises(Exception, match="Unauthorized"):
            mod.handler(event, None)

    def test_authorizer_malformed_jwt_raises_unauthorized(self):
        import pytest as _pytest
        mod = self._import_authorizer()
        event = {
            "authorizationToken": "Bearer not.valid",
            "methodArn": "arn:aws:execute-api:us-east-1:123456:api/prod/POST/tools/analyze",
        }
        with _pytest.raises(Exception, match="Unauthorized"):
            mod.handler(event, None)


# ---------------------------------------------------------------------------
# Extract tool (#38, #39)
# ---------------------------------------------------------------------------

def _extract_routing_config():
    """Routing config with extract tool and structured_output capability."""
    return {
        "routing": {
            "extract": {
                "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0", "openai/gpt-4o"],
                "system_prompt": "You are a structured data extraction specialist.",
            },
            "analyze": {
                "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"],
                "system_prompt": "You are an expert analyst.",
            },
        },
        "defaults": {"max_tokens": 4096, "temperature": 0.7},
        "model_capabilities": {
            "bedrock/anthropic.claude-sonnet-4-20250514-v1:0": ["structured_output"],
            "openai/gpt-4o": ["structured_output"],
        },
        "model_context_windows": {
            "bedrock/anthropic.claude-sonnet-4-20250514-v1:0": 200000,
            "openai/gpt-4o": 128000,
        },
    }


class TestExtractTool:
    def test_happy_path_returns_extracted_fields(self, provider_functions, provider_secrets):
        cfg = _extract_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        provider_resp = {
            "content": '{"effect_sizes": [{"measure": "d", "value": 0.5}]}',
            "extracted_fields": {"effect_sizes": [{"measure": "d", "value": 0.5}]},
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 200, "output_tokens": 80,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(provider_resp)), \
             patch.object(h, "spend_record_write"):
            event = {"tool": "extract", "body": json.dumps({
                "prompt": "Extract from this paper.",
                "extraction_types": ["effect_sizes"],
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "extracted_fields" in body
        assert body["extracted_fields"]["effect_sizes"][0]["measure"] == "d"

    def test_missing_extraction_types_returns_400(self, provider_functions, provider_secrets):
        cfg = _extract_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"bedrock"}):
            event = {"tool": "extract", "body": json.dumps({"prompt": "Extract something."})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "extraction_types" in body["error"]

    def test_structured_output_capability_required(self, provider_functions, provider_secrets):
        cfg = {
            "routing": {
                "extract": {"preferred": ["openai/gpt-3.5-turbo"], "system_prompt": "extract"},
                "analyze": {"preferred": ["openai/gpt-3.5-turbo"], "system_prompt": "analyze"},
            },
            "defaults": {"max_tokens": 4096, "temperature": 0.7},
            "model_capabilities": {"openai/gpt-3.5-turbo": []},  # no structured_output
            "model_context_windows": {"openai/gpt-3.5-turbo": 16000},
        }
        h = _load_handler(cfg, provider_functions, provider_secrets)
        with patch.object(h, "_available_providers", {"openai"}):
            event = {"tool": "extract", "body": json.dumps({
                "prompt": "Extract this.", "extraction_types": ["citations"],
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["code"] == "unsatisfiable_capabilities"

    def test_open_problems_store_at_uri_writes_s3(self, provider_functions, provider_secrets):
        cfg = _extract_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        open_problems_data = [{"gap_statement": "Mechanism unknown", "domain": "biology", "confidence": 0.9}]
        provider_resp = {
            "content": json.dumps({"open_problems": open_problems_data}),
            "extracted_fields": {"open_problems": open_problems_data},
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 200, "output_tokens": 80,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(provider_resp)), \
             patch.object(h, "spend_record_write"), \
             patch("boto3.client") as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.return_value = mock_s3
            event = {"tool": "extract", "body": json.dumps({
                "prompt": "Extract gaps.",
                "extraction_types": ["open_problems"],
                "store_at_uri": "s3://my-bucket/gaps/problems.json",
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body.get("stored_at_uri") == "s3://my-bucket/gaps/problems.json"

    def test_open_problems_type_extraction(self, provider_functions, provider_secrets):
        cfg = _extract_routing_config()
        h = _load_handler(cfg, provider_functions, provider_secrets)
        provider_resp = {
            "content": json.dumps({"open_problems": [{"gap_statement": "X is unknown", "domain": "physics", "confidence": 0.8}]}),
            "extracted_fields": {"open_problems": [{"gap_statement": "X is unknown", "domain": "physics", "confidence": 0.8}]},
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 150, "output_tokens": 60,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(provider_resp)), \
             patch.object(h, "spend_record_write"):
            event = {"tool": "extract", "body": json.dumps({
                "prompt": "Find open problems.",
                "extraction_types": ["open_problems"],
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        gaps = body["extracted_fields"]["open_problems"]
        assert len(gaps) == 1
        assert "gap_statement" in gaps[0]


# ---------------------------------------------------------------------------
# Grounding mode strict (#40)
# ---------------------------------------------------------------------------

class TestGroundingModeStrict:
    def test_strict_mode_response_includes_grounding_fields(self, provider_functions, provider_secrets):
        cfg = {
            "routing": {
                "research": {
                    "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"],
                    "system_prompt": "You are a research assistant.",
                },
                "analyze": {"preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"], "system_prompt": ""},
            },
            "defaults": {"max_tokens": 4096, "temperature": 0.7},
            "model_capabilities": {"bedrock/anthropic.claude-sonnet-4-20250514-v1:0": []},
            "model_context_windows": {"bedrock/anthropic.claude-sonnet-4-20250514-v1:0": 200000},
        }
        h = _load_handler(cfg, provider_functions, provider_secrets)
        provider_resp = {
            "content": "This paper claims X [source: p.3].",
            "sources_used": ["Section 2", "p.3"],
            "grounding_coverage": 0.85,
            "low_confidence_claims": [],
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 300, "output_tokens": 120,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(provider_resp)), \
             patch.object(h, "spend_record_write"):
            event = {"tool": "research", "body": json.dumps({
                "prompt": "Summarize this paper.",
                "grounding_mode": "strict",
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "sources_used" in body
        assert "grounding_coverage" in body
        assert "low_confidence_claims" in body
        assert body["grounding_coverage"] == 0.85

    def test_default_mode_omits_grounding_fields(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        provider_resp = {
            "content": "Research result.",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100, "output_tokens": 50,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(provider_resp)), \
             patch.object(h, "spend_record_write"):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Analyze this."})}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "sources_used" not in body
        assert "grounding_coverage" not in body

    def test_invalid_grounding_mode_treated_as_default(self, routing_config, provider_functions, provider_secrets):
        h = _load_handler(routing_config, provider_functions, provider_secrets)
        provider_resp = {
            "content": "Result.",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100, "output_tokens": 50,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(provider_resp)), \
             patch.object(h, "spend_record_write"):
            event = {"tool": "analyze", "body": json.dumps({
                "prompt": "Hello.", "grounding_mode": "invalid_mode",
            })}
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 200
