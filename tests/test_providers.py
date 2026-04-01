"""
Tests for provider Lambda handlers.

Each provider is tested for:
- Successful invocation with mocked API response
- Rate limit (429) handling
- Auth failure (401/403) handling
- Timeout handling
- Empty prompt returns error
- Missing credentials returns error (external providers)
- Response schema has all expected fields
- Context prepend (TODO §2)
- Guardrail input block (TODO §1)
- Guardrail output block (TODO §1)
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
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
import io

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "common", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "providers"))

EXPECTED_FIELDS = {"content", "provider", "model", "input_tokens", "output_tokens",
                   "guardrail_applied", "guardrail_blocked"}


def _http_response(body: dict, status: int = 200):
    """Create a mock urllib response."""
    encoded = json.dumps(body).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = encoded
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _http_error(code: int, msg: str = "Error"):
    err = HTTPError(url="", code=code, msg=msg, hdrs={}, fp=io.BytesIO(b"error body"))
    return err


def _reload_with_env(module_name, env):
    with patch.dict(os.environ, env):
        mod = importlib.import_module(module_name)
        importlib.reload(mod)
        return mod


# ===========================================================================
# Anthropic
# ===========================================================================

class TestAnthropicProvider:
    MODULE = "anthropic_provider"
    ENV = {
        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }
    API_SUCCESS = {
        "id": "msg-123",
        "content": [{"type": "text", "text": "Analysis complete."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def _no_guardrail(self, t, *a, **kw):
        return t, False

    def _block_input(self, t, gid, gv, source="INPUT"):
        return ("Content blocked by policy.", True) if source == "INPUT" else (t, False)

    def _block_output(self, t, gid, gv, source="INPUT"):
        return ("Response blocked by content policy.", True) if source == "OUTPUT" else (t, False)

    def test_successful_invocation(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_http_response(self.API_SUCCESS)):
            result = p.handler({"prompt": "Analyze this"}, None)
        assert EXPECTED_FIELDS.issubset(result.keys())
        assert result["content"] == "Analysis complete."
        assert result["provider"] == "anthropic"
        assert result["guardrail_applied"] is True
        assert result["guardrail_blocked"] is False

    def test_empty_prompt_returns_error(self):
        p = self._provider()
        result = p.handler({"prompt": ""}, None)
        assert "error" in result
        assert result["content"] == ""

    def test_missing_credentials_returns_error(self):
        p = _reload_with_env(self.MODULE, {"SECRET_ARN": "", "GUARDRAIL_ID": ""})
        result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result
        assert "not configured" in result["error"]

    def test_rate_limit_429(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(429)):
            result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result
        assert "Rate limited" in result["error"]

    def test_auth_failure_401(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-bad"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(401)):
            result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result
        assert "Invalid" in result["error"]

    def test_timeout_returns_error(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result

    def test_context_prepended_to_prompt(self):
        p = self._provider()
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _http_response(self.API_SUCCESS)
        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            p.handler({"prompt": "Summarize", "context": "Revenue grew 20%"}, None)
        msg_content = captured["body"]["messages"][0]["content"]
        assert "Revenue grew 20%" in msg_content
        assert "Summarize" in msg_content

    def test_guardrail_input_block_returns_early(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail", side_effect=self._block_input):
            result = p.handler({"prompt": "Bad content"}, None)
        assert result["guardrail_blocked"] is True
        assert result["input_tokens"] == 0

    def test_guardrail_output_block(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail", side_effect=self._block_output), \
             patch("urllib.request.urlopen", return_value=_http_response(self.API_SUCCESS)):
            result = p.handler({"prompt": "Hello"}, None)
        assert result["guardrail_blocked"] is True
        assert "blocked" in result["content"].lower()


# ===========================================================================
# OpenAI
# ===========================================================================

class TestOpenAIProvider:
    MODULE = "openai_provider"
    ENV = {
        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }
    API_SUCCESS = {
        "id": "chatcmpl-123",
        "model": "gpt-4o",
        "choices": [{"message": {"content": "Analysis complete."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def _no_guardrail(self, t, *a, **kw):
        return t, False

    def _block_input(self, t, gid, gv, source="INPUT"):
        return ("Content blocked.", True) if source == "INPUT" else (t, False)

    def test_successful_invocation(self):
        p = self._provider()
        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_http_response(self.API_SUCCESS)):
            result = p.handler({"prompt": "Analyze this"}, None)
        assert EXPECTED_FIELDS.issubset(result.keys())
        assert result["content"] == "Analysis complete."
        assert result["provider"] == "openai"

    def test_empty_prompt_returns_error(self):
        p = self._provider()
        result = p.handler({"prompt": ""}, None)
        assert "error" in result

    def test_missing_credentials_returns_error(self):
        p = _reload_with_env(self.MODULE, {"SECRET_ARN": "", "GUARDRAIL_ID": ""})
        result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result
        assert "not configured" in result["error"]

    def test_rate_limit_429(self):
        p = self._provider()
        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(429)):
            result = p.handler({"prompt": "Hello"}, None)
        assert "Rate limited" in result["error"]

    def test_auth_failure_401(self):
        p = self._provider()
        with patch.object(p, "_creds", {"api_key": "sk-bad"}), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(401)):
            result = p.handler({"prompt": "Hello"}, None)
        assert "Invalid" in result["error"]

    def test_timeout_returns_error(self):
        p = self._provider()
        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result

    def test_context_prepended_to_prompt(self):
        p = self._provider()
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _http_response(self.API_SUCCESS)
        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            p.handler({"prompt": "Summarize", "context": "Q3 revenue up"}, None)
        user_msg = captured["body"]["messages"][-1]["content"]
        assert "Q3 revenue up" in user_msg

    def test_guardrail_input_block_returns_early(self):
        p = self._provider()
        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail", side_effect=self._block_input):
            result = p.handler({"prompt": "Bad content"}, None)
        assert result["guardrail_blocked"] is True
        assert result["input_tokens"] == 0


# ===========================================================================
# Gemini
# ===========================================================================

class TestGeminiProvider:
    MODULE = "gemini_provider"
    ENV = {
        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }
    API_SUCCESS = {
        "candidates": [{"content": {"parts": [{"text": "Analysis complete."}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
        "modelVersion": "gemini-2.5-pro-001",
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def _no_guardrail(self, t, *a, **kw):
        return t, False

    def _block_input(self, t, gid, gv, source="INPUT"):
        return ("Content blocked.", True) if source == "INPUT" else (t, False)

    def test_successful_invocation(self):
        p = self._provider()
        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_http_response(self.API_SUCCESS)):
            result = p.handler({"prompt": "Analyze this"}, None)
        assert EXPECTED_FIELDS.issubset(result.keys())
        assert result["content"] == "Analysis complete."
        assert result["provider"] == "gemini"

    def test_empty_prompt_returns_error(self):
        p = self._provider()
        result = p.handler({"prompt": ""}, None)
        assert "error" in result

    def test_missing_credentials_returns_error(self):
        p = _reload_with_env(self.MODULE, {"SECRET_ARN": "", "GUARDRAIL_ID": ""})
        result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result

    def test_rate_limit_429(self):
        p = self._provider()
        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(429)):
            result = p.handler({"prompt": "Hello"}, None)
        assert "Rate limited" in result["error"]

    def test_auth_failure_403(self):
        p = self._provider()
        with patch.object(p, "_api_key", "bad-key"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(403)):
            result = p.handler({"prompt": "Hello"}, None)
        assert "invalid" in result["error"].lower() or "not enabled" in result["error"].lower()

    def test_timeout_returns_error(self):
        p = self._provider()
        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result

    def test_context_prepended_to_prompt(self):
        p = self._provider()
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _http_response(self.API_SUCCESS)
        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            p.handler({"prompt": "Summarize", "context": "Enrollment dropped 5%"}, None)
        user_text = captured["body"]["contents"][0]["parts"][0]["text"]
        assert "Enrollment dropped 5%" in user_text

    def test_guardrail_input_block_returns_early(self):
        p = self._provider()
        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail", side_effect=self._block_input):
            result = p.handler({"prompt": "Bad content"}, None)
        assert result["guardrail_blocked"] is True
        assert result["input_tokens"] == 0


# ===========================================================================
# Bedrock
# ===========================================================================

class TestBedrockProvider:
    MODULE = "bedrock_provider"
    ENV = {
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }
    CONVERSE_SUCCESS = {
        "output": {"message": {"content": [{"text": "Analysis complete."}]}},
        "usage": {"inputTokens": 100, "outputTokens": 50},
        "stopReason": "end_turn",
        "ResponseMetadata": {"RequestId": "req-123"},
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def test_successful_invocation(self):
        p = self._provider()
        p.bedrock.converse = MagicMock(return_value=self.CONVERSE_SUCCESS)
        result = p.handler({"prompt": "Analyze this"}, None)
        assert EXPECTED_FIELDS.issubset(result.keys())
        assert result["content"] == "Analysis complete."
        assert result["provider"] == "bedrock"
        assert result["guardrail_applied"] is True

    def test_empty_prompt_returns_error(self):
        p = self._provider()
        result = p.handler({"prompt": ""}, None)
        assert "error" in result

    def test_throttling_returns_error(self):
        p = self._provider()
        p.bedrock.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
        p.bedrock.converse = MagicMock(side_effect=p.bedrock.exceptions.ThrottlingException("throttled"))
        result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result
        assert "Rate limited" in result["error"]

    def test_validation_error_returns_error(self):
        p = self._provider()
        p.bedrock.exceptions.ValidationException = type("ValidationException", (Exception,), {})
        p.bedrock.converse = MagicMock(side_effect=p.bedrock.exceptions.ValidationException("invalid"))
        result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result

    def test_guardrail_intervened_sets_blocked(self):
        p = self._provider()
        blocked_resp = {**self.CONVERSE_SUCCESS, "stopReason": "guardrail_intervened"}
        p.bedrock.converse = MagicMock(return_value=blocked_resp)
        result = p.handler({"prompt": "Bad content"}, None)
        assert result["guardrail_blocked"] is True

    def test_context_prepended_to_prompt(self):
        p = self._provider()
        captured = {}
        def capture_converse(**kwargs):
            captured["params"] = kwargs
            return self.CONVERSE_SUCCESS
        p.bedrock.converse = MagicMock(side_effect=capture_converse)
        p.handler({"prompt": "Summarize", "context": "Revenue data here"}, None)
        msg_text = captured["params"]["messages"][0]["content"][0]["text"]
        assert "Revenue data here" in msg_text
        assert "Summarize" in msg_text

    def test_timeout_returns_error(self):
        p = self._provider()
        p.bedrock.converse = MagicMock(side_effect=TimeoutError("timed out"))
        result = p.handler({"prompt": "Hello"}, None)
        assert "error" in result

    def test_response_schema_complete(self):
        p = self._provider()
        p.bedrock.converse = MagicMock(return_value=self.CONVERSE_SUCCESS)
        result = p.handler({"prompt": "Test"}, None)
        assert EXPECTED_FIELDS.issubset(result.keys())
        assert "metadata" in result
