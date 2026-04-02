"""
Integration tests for Bedrock Guardrail application via Substrate.

These tests verify that apply_guardrail() makes real HTTP calls to the
bedrock-runtime endpoint and that provider handlers correctly pass the
guardrail result through. They run against Substrate 0.45.1+ which
provides the BedrockRuntimePlugin with ApplyGuardrail support.

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

pytestmark = pytest.mark.integration

GUARDRAIL_ID = "qs-test-guardrail"


def _mock_http_response(body: dict):
    encoded = json.dumps(body).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = encoded
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ===========================================================================
# apply_guardrail() direct tests — no provider involved
# ===========================================================================

class TestApplyGuardrailDirect:
    """Verify apply_guardrail() makes the real boto3 → bedrock-runtime → Substrate call."""

    def _reload_pi(self, monkeypatch, substrate_url):
        """Reload provider_interface with AWS_ENDPOINT_URL pointing at Substrate."""
        monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
        import provider_interface
        importlib.reload(provider_interface)
        return provider_interface

    def test_passthrough_input(self, substrate_url, reset_substrate, monkeypatch):
        pi = self._reload_pi(monkeypatch, substrate_url)
        text, blocked = pi.apply_guardrail("Hello world", GUARDRAIL_ID, "DRAFT", source="INPUT")
        assert text == "Hello world"
        assert blocked is False

    def test_passthrough_output(self, substrate_url, reset_substrate, monkeypatch):
        pi = self._reload_pi(monkeypatch, substrate_url)
        payload = "Revenue grew 20% year-over-year."
        text, blocked = pi.apply_guardrail(payload, GUARDRAIL_ID, "DRAFT", source="OUTPUT")
        assert text == payload
        assert blocked is False

    def test_empty_guardrail_id_skips_http(self, substrate_url, monkeypatch):
        """Empty guardrail_id must short-circuit before making any HTTP call."""
        pi = self._reload_pi(monkeypatch, substrate_url)
        text, blocked = pi.apply_guardrail("test prompt", "", "DRAFT", source="INPUT")
        assert text == "test prompt"
        assert blocked is False

    def test_fail_open_on_bad_endpoint(self, monkeypatch):
        """apply_guardrail must fail-open (return original text, False) if endpoint is unreachable."""
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:19999")
        import provider_interface
        importlib.reload(provider_interface)
        text, blocked = provider_interface.apply_guardrail("test", GUARDRAIL_ID, "DRAFT")
        assert text == "test"
        assert blocked is False


# ===========================================================================
# Provider handler tests with real guardrail round-trip
# ===========================================================================

class TestAnthropicWithRealGuardrail:
    """Anthropic provider calls Substrate guardrail on input and output."""

    API_SUCCESS = {
        "id": "msg-123",
        "content": [{"type": "text", "text": "Analysis complete."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }

    def _load_provider(self, substrate_url, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
        import provider_interface
        importlib.reload(provider_interface)
        env = {
            "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            "GUARDRAIL_ID": GUARDRAIL_ID,
            "GUARDRAIL_VERSION": "DRAFT",
        }
        with patch.dict(os.environ, env):
            import anthropic_provider
            importlib.reload(anthropic_provider)
        return anthropic_provider

    def test_provider_passes_with_real_guardrail(self, substrate_url, reset_substrate, monkeypatch):
        p = self._load_provider(substrate_url, monkeypatch)
        with patch.object(p, "_api_key", "sk-test"), \
             patch("urllib.request.urlopen", return_value=_mock_http_response(self.API_SUCCESS)):
            result = p.handler({"prompt": "Analyze our Q3 results"}, None)

        assert result["content"] == "Analysis complete."
        assert result["guardrail_applied"] is True
        assert result["guardrail_blocked"] is False
        assert result["provider"] == "anthropic"

    def test_context_plus_guardrail(self, substrate_url, reset_substrate, monkeypatch):
        p = self._load_provider(substrate_url, monkeypatch)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _mock_http_response(self.API_SUCCESS)

        with patch.object(p, "_api_key", "sk-test"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = p.handler({"prompt": "Summarize", "context": "Q3 revenue: $10M"}, None)

        assert result["guardrail_blocked"] is False
        # Context must have been prepended before the guardrail saw it
        user_content = captured["body"]["messages"][0]["content"]
        assert "Q3 revenue: $10M" in user_content


class TestOpenAIWithRealGuardrail:
    API_SUCCESS = {
        "id": "chatcmpl-456",
        "model": "gpt-4o",
        "choices": [{"message": {"content": "Report looks good."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 20},
    }

    def _load_provider(self, substrate_url, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
        import provider_interface
        importlib.reload(provider_interface)
        env = {
            "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            "GUARDRAIL_ID": GUARDRAIL_ID,
            "GUARDRAIL_VERSION": "DRAFT",
        }
        with patch.dict(os.environ, env):
            import openai_provider
            importlib.reload(openai_provider)
        return openai_provider

    def test_provider_passes_with_real_guardrail(self, substrate_url, reset_substrate, monkeypatch):
        p = self._load_provider(substrate_url, monkeypatch)
        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch("urllib.request.urlopen", return_value=_mock_http_response(self.API_SUCCESS)):
            result = p.handler({"prompt": "Summarize this report"}, None)

        assert result["content"] == "Report looks good."
        assert result["guardrail_applied"] is True
        assert result["guardrail_blocked"] is False


class TestGeminiWithRealGuardrail:
    API_SUCCESS = {
        "candidates": [{"content": {"parts": [{"text": "Summary done."}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 15},
        "modelVersion": "gemini-2.5-pro-001",
    }

    def _load_provider(self, substrate_url, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
        import provider_interface
        importlib.reload(provider_interface)
        env = {
            "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            "GUARDRAIL_ID": GUARDRAIL_ID,
            "GUARDRAIL_VERSION": "DRAFT",
        }
        with patch.dict(os.environ, env):
            import gemini_provider
            importlib.reload(gemini_provider)
        return gemini_provider

    def test_provider_passes_with_real_guardrail(self, substrate_url, reset_substrate, monkeypatch):
        p = self._load_provider(substrate_url, monkeypatch)
        with patch.object(p, "_api_key", "goog-key"), \
             patch("urllib.request.urlopen", return_value=_mock_http_response(self.API_SUCCESS)):
            result = p.handler({"prompt": "Summarize enrollment trends"}, None)

        assert result["content"] == "Summary done."
        assert result["guardrail_applied"] is True
        assert result["guardrail_blocked"] is False
