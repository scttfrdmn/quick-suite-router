"""
Tests for SSE streaming support (v0.6.0).

Covers:
- Router: stream=True forwarded for generate/research tools only
- Router: stream=True ignored for other tools with log
- Router: streaming response includes chunks and streaming=True
- Router: cache still written for streaming responses (low temperature)
- Anthropic: streaming path collects chunks, assembles content
- Anthropic: streaming path applies guardrail to assembled text
- Anthropic: streaming path handles 429/401 HTTP errors
- OpenAI: streaming path collects chunks, assembles content
- OpenAI: streaming path applies guardrail to assembled text
- OpenAI: streaming path handles HTTP errors
- Bedrock: streaming path collects chunks from converse_stream()
- Bedrock: streaming path applies guardrail to assembled text
- Bedrock: streaming path handles throttling error
- Gemini: streaming path collects chunks from SSE response
- Gemini: streaming path applies guardrail to assembled text
- Gemini: streaming path handles HTTP errors
"""

import importlib
import io
import json
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "common", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "providers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "router"))

EXPECTED_FIELDS = {"content", "provider", "model", "input_tokens", "output_tokens",
                   "guardrail_applied", "guardrail_blocked"}
STREAM_EXTRA_FIELDS = {"chunks", "streaming"}


def _reload_with_env(module_name, env):
    with patch.dict(os.environ, env):
        mod = importlib.import_module(module_name)
        importlib.reload(mod)
        return mod


def _make_lambda_payload(data: dict):
    payload_mock = MagicMock()
    payload_mock.read.return_value = json.dumps(data).encode()
    response_mock = MagicMock()
    response_mock.__getitem__ = MagicMock(side_effect=lambda k: payload_mock if k == "Payload" else None)
    return response_mock


def _load_handler(routing_config, provider_functions, provider_secrets, cache_table=""):
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


def _sse_response(lines: list[str]):
    """Build a mock urllib response that yields SSE lines."""
    encoded_lines = [f"{line}\n".encode() for line in lines]
    mock_resp = MagicMock()
    mock_resp.__iter__ = MagicMock(return_value=iter(encoded_lines))
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _http_error(code: int, msg: str = "Error"):
    return HTTPError(url="", code=code, msg=msg, hdrs={}, fp=io.BytesIO(b"error body"))


# ---------------------------------------------------------------------------
# Router-level streaming tests
# ---------------------------------------------------------------------------

class TestRouterStreaming:
    BASE_CONFIG = {
        "routing": {
            "generate": {
                "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"],
                "system_prompt": "You are a creative writer.",
            },
            "research": {
                "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"],
                "system_prompt": "You are a researcher.",
            },
            "analyze": {
                "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"],
                "system_prompt": "You are an analyst.",
            },
        },
        "defaults": {"max_tokens": 4096, "temperature": 0.7},
    }
    PROVIDER_FUNCTIONS = {
        "bedrock": "arn:aws:lambda:us-east-1:123456789012:function:qs-router-provider-bedrock",
    }
    PROVIDER_SECRETS = {}

    def test_stream_flag_forwarded_for_generate_tool(self):
        h = _load_handler(self.BASE_CONFIG, self.PROVIDER_FUNCTIONS, self.PROVIDER_SECRETS)
        captured_payload = {}

        streaming_resp = {
            "content": "Hello world",
            "chunks": ["Hello", " world"],
            "streaming": True,
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 10,
            "output_tokens": 5,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

        def capture_invoke(**kwargs):
            captured_payload.update(json.loads(kwargs["Payload"]))
            return _make_lambda_payload(streaming_resp)

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", side_effect=capture_invoke):
            event = {"tool": "generate", "body": json.dumps({"prompt": "Write a poem", "stream": True})}
            result = h.handle_tool_invocation(event)

        assert captured_payload.get("stream") is True
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body.get("streaming") is True
        assert "chunks" in body

    def test_stream_flag_forwarded_for_research_tool(self):
        h = _load_handler(self.BASE_CONFIG, self.PROVIDER_FUNCTIONS, self.PROVIDER_SECRETS)
        captured_payload = {}

        streaming_resp = {
            "content": "Research findings",
            "chunks": ["Research", " findings"],
            "streaming": True,
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 10,
            "output_tokens": 5,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

        def capture_invoke(**kwargs):
            captured_payload.update(json.loads(kwargs["Payload"]))
            return _make_lambda_payload(streaming_resp)

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", side_effect=capture_invoke):
            event = {"tool": "research", "body": json.dumps({"prompt": "Research AI trends", "stream": True})}
            h.handle_tool_invocation(event)

        assert captured_payload.get("stream") is True

    def test_stream_ignored_for_non_streaming_tools(self):
        h = _load_handler(self.BASE_CONFIG, self.PROVIDER_FUNCTIONS, self.PROVIDER_SECRETS)
        captured_payload = {}

        success_resp = {
            "content": "Analysis result",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 10,
            "output_tokens": 5,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

        def capture_invoke(**kwargs):
            captured_payload.update(json.loads(kwargs["Payload"]))
            return _make_lambda_payload(success_resp)

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", side_effect=capture_invoke):
            event = {"tool": "analyze", "body": json.dumps({"prompt": "Analyze this", "stream": True})}
            h.handle_tool_invocation(event)

        # stream flag must be False when tool is not generate/research
        assert captured_payload.get("stream") is False

    def test_stream_false_not_set_for_non_streaming_tool(self):
        """When stream is not requested at all, stream=False forwarded."""
        h = _load_handler(self.BASE_CONFIG, self.PROVIDER_FUNCTIONS, self.PROVIDER_SECRETS)
        captured_payload = {}

        success_resp = {
            "content": "ok",
            "provider": "bedrock",
            "model": "m",
            "input_tokens": 0,
            "output_tokens": 0,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

        def capture_invoke(**kwargs):
            captured_payload.update(json.loads(kwargs["Payload"]))
            return _make_lambda_payload(success_resp)

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", side_effect=capture_invoke):
            event = {"tool": "generate", "body": json.dumps({"prompt": "Write something"})}
            h.handle_tool_invocation(event)

        assert captured_payload.get("stream") is False

    def test_streaming_response_cached_at_low_temperature(self):
        h = _load_handler(
            self.BASE_CONFIG, self.PROVIDER_FUNCTIONS, self.PROVIDER_SECRETS,
            cache_table="qs-router-cache"
        )

        streaming_resp = {
            "content": "Streamed content",
            "chunks": ["Streamed", " content"],
            "streaming": True,
            "provider": "bedrock",
            "model": "m",
            "input_tokens": 5,
            "output_tokens": 5,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

        with patch.object(h, "cache_get", return_value=None), \
             patch.object(h, "cache_put") as mock_put, \
             patch.object(h, "emit_usage_metrics"), \
             patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h.lambda_client, "invoke", return_value=_make_lambda_payload(streaming_resp)):
            event = {"tool": "generate", "body": json.dumps({
                "prompt": "Write a poem", "temperature": 0.0, "stream": True
            })}
            h.handle_tool_invocation(event)

        mock_put.assert_called_once()


# ---------------------------------------------------------------------------
# Anthropic streaming tests
# ---------------------------------------------------------------------------

class TestAnthropicStreaming:
    MODULE = "anthropic_provider"
    ENV = {
        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def _no_guardrail(self, t, *a, **kw):
        return t, False

    def _block_output(self, t, gid, gv, source="INPUT"):
        return ("Output blocked.", True) if source == "OUTPUT" else (t, False)

    def _make_sse_lines(self):
        """Build realistic Anthropic SSE lines."""
        return [
            'data: {"type": "message_start", "message": {"id": "msg-abc", "usage": {"input_tokens": 15}}}',
            'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}',
            'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}',
            'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}}',
            'data: {"type": "content_block_stop", "index": 0}',
            'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}',
            'data: {"type": "message_stop"}',
        ]

    def test_streaming_returns_chunks_and_content(self):
        p = self._provider()
        sse_lines = self._make_sse_lines()

        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert EXPECTED_FIELDS.issubset(result.keys())
        assert STREAM_EXTRA_FIELDS.issubset(result.keys())
        assert result["streaming"] is True
        assert result["content"] == "Hello world"
        assert result["chunks"] == ["Hello", " world"]
        assert result["provider"] == "anthropic"
        assert result["input_tokens"] == 15
        assert result["output_tokens"] == 5

    def test_streaming_guardrail_applied_to_assembled_text(self):
        p = self._provider()
        sse_lines = self._make_sse_lines()

        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._block_output), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert result["guardrail_blocked"] is True
        assert "blocked" in result["content"].lower()

    def test_streaming_handles_rate_limit(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(429)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "error" in result
        assert "Rate limited" in result["error"]

    def test_streaming_handles_auth_error(self):
        p = self._provider()
        with patch.object(p, "_api_key", "sk-bad"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(401)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "error" in result
        assert "Invalid" in result["error"]

    def test_non_streaming_still_works(self):
        """Ensure non-streaming path is unaffected."""
        p = self._provider()
        api_success = {
            "id": "msg-xyz",
            "content": [{"type": "text", "text": "Non-stream response"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 8},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_success).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = p.handler({"prompt": "Hello"}, None)

        assert result["content"] == "Non-stream response"
        assert "chunks" not in result
        assert "streaming" not in result

    def test_streaming_skips_non_delta_events(self):
        """Lines that are not content_block_delta must not add to chunks."""
        p = self._provider()
        # Only message_start and message_stop, no deltas
        sse_lines = [
            'data: {"type": "message_start", "message": {"id": "msg-empty", "usage": {"input_tokens": 5}}}',
            'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 0}}',
            'data: {"type": "message_stop"}',
        ]

        with patch.object(p, "_api_key", "sk-test"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Empty response", "stream": True}, None)

        assert result["chunks"] == []
        assert result["content"] == ""
        assert result["streaming"] is True


# ---------------------------------------------------------------------------
# OpenAI streaming tests
# ---------------------------------------------------------------------------

class TestOpenAIStreaming:
    MODULE = "openai_provider"
    ENV = {
        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def _no_guardrail(self, t, *a, **kw):
        return t, False

    def _block_output(self, t, gid, gv, source="INPUT"):
        return ("Output blocked.", True) if source == "OUTPUT" else (t, False)

    def _make_sse_lines(self):
        """Build realistic OpenAI SSE lines."""
        return [
            'data: {"id": "chatcmpl-123", "model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}, "finish_reason": null}]}',
            'data: {"id": "chatcmpl-123", "model": "gpt-4o", "choices": [{"delta": {"content": " world"}, "finish_reason": null}]}',
            'data: {"id": "chatcmpl-123", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}',
            'data: [DONE]',
        ]

    def test_streaming_returns_chunks_and_content(self):
        p = self._provider()
        sse_lines = self._make_sse_lines()

        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert EXPECTED_FIELDS.issubset(result.keys())
        assert STREAM_EXTRA_FIELDS.issubset(result.keys())
        assert result["streaming"] is True
        assert result["content"] == "Hello world"
        assert result["chunks"] == ["Hello", " world"]
        assert result["provider"] == "openai"
        assert result["input_tokens"] == 10
        assert result["output_tokens"] == 4

    def test_streaming_guardrail_applied_to_assembled_text(self):
        p = self._provider()
        sse_lines = self._make_sse_lines()

        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._block_output), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert result["guardrail_blocked"] is True

    def test_streaming_handles_rate_limit(self):
        p = self._provider()
        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(429)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "Rate limited" in result["error"]

    def test_streaming_handles_auth_error(self):
        p = self._provider()
        with patch.object(p, "_creds", {"api_key": "sk-bad"}), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(401)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "Invalid" in result["error"]

    def test_streaming_done_terminates_loop(self):
        p = self._provider()
        # [DONE] should stop parsing
        sse_lines = [
            'data: {"id": "c1", "model": "gpt-4o", "choices": [{"delta": {"content": "First"}, "finish_reason": null}]}',
            'data: [DONE]',
            # This line would be ignored after [DONE]
            'data: {"id": "c1", "choices": [{"delta": {"content": "SHOULD_NOT_APPEAR"}}]}',
        ]

        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert "SHOULD_NOT_APPEAR" not in result["content"]
        assert result["chunks"] == ["First"]

    def test_non_streaming_still_works(self):
        p = self._provider()
        api_success = {
            "id": "chatcmpl-xyz",
            "model": "gpt-4o",
            "choices": [{"message": {"content": "Non-stream"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_success).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(p, "_creds", {"api_key": "sk-test"}), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = p.handler({"prompt": "Hello"}, None)

        assert result["content"] == "Non-stream"
        assert "chunks" not in result


# ---------------------------------------------------------------------------
# Bedrock streaming tests
# ---------------------------------------------------------------------------

class TestBedrockStreaming:
    MODULE = "bedrock_provider"
    ENV = {
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def _make_stream_events(self):
        """Build a list of converse_stream event dicts."""
        return [
            {"contentBlockDelta": {"blockIndex": 0, "delta": {"text": "Bedrock"}}},
            {"contentBlockDelta": {"blockIndex": 0, "delta": {"text": " streaming"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 6}}},
        ]

    def test_streaming_returns_chunks_and_content(self):
        p = self._provider()
        events = self._make_stream_events()

        mock_stream = iter(events)
        converse_stream_resp = {
            "stream": mock_stream,
            "ResponseMetadata": {"RequestId": "req-stream-123"},
        }
        p.bedrock.converse_stream = MagicMock(return_value=converse_stream_resp)

        # apply_guardrail should pass through
        with patch.object(p, "apply_guardrail_safe", side_effect=lambda t, *a, **kw: (t, False)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert EXPECTED_FIELDS.issubset(result.keys())
        assert STREAM_EXTRA_FIELDS.issubset(result.keys())
        assert result["streaming"] is True
        assert result["content"] == "Bedrock streaming"
        assert result["chunks"] == ["Bedrock", " streaming"]
        assert result["provider"] == "bedrock"
        assert result["input_tokens"] == 20
        assert result["output_tokens"] == 6

    def test_streaming_guardrail_applied_to_assembled_text(self):
        p = self._provider()
        events = self._make_stream_events()
        converse_stream_resp = {
            "stream": iter(events),
            "ResponseMetadata": {"RequestId": "req-x"},
        }
        p.bedrock.converse_stream = MagicMock(return_value=converse_stream_resp)

        with patch.object(p, "apply_guardrail_safe", return_value=("Blocked text.", True)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert result["guardrail_blocked"] is True

    def test_streaming_guardrail_intervened_via_stop_reason(self):
        """If converse_stream returns guardrail_intervened stopReason, blocked=True."""
        p = self._provider()
        events = [
            {"contentBlockDelta": {"blockIndex": 0, "delta": {"text": "Bad"}}},
            {"messageStop": {"stopReason": "guardrail_intervened"}},
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 1}}},
        ]
        converse_stream_resp = {
            "stream": iter(events),
            "ResponseMetadata": {"RequestId": "req-blocked"},
        }
        p.bedrock.converse_stream = MagicMock(return_value=converse_stream_resp)

        # apply_guardrail pass-through (guardrail_blocked already set from stopReason)
        with patch.object(p, "apply_guardrail_safe", side_effect=lambda t, *a, **kw: (t, False)):
            result = p.handler({"prompt": "Bad content", "stream": True}, None)

        assert result["guardrail_blocked"] is True

    def test_streaming_handles_throttling(self):
        p = self._provider()
        p.bedrock.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
        p.bedrock.converse_stream = MagicMock(
            side_effect=p.bedrock.exceptions.ThrottlingException("throttled")
        )
        result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "error" in result
        assert "Rate limited" in result["error"]

    def test_streaming_handles_validation_error(self):
        p = self._provider()
        p.bedrock.exceptions.ValidationException = type("ValidationException", (Exception,), {})
        p.bedrock.converse_stream = MagicMock(
            side_effect=p.bedrock.exceptions.ValidationException("invalid")
        )
        result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "error" in result

    def test_non_streaming_still_works(self):
        """Ensure non-streaming converse() path is unaffected."""
        p = self._provider()
        converse_success = {
            "output": {"message": {"content": [{"text": "Non-stream result"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
            "ResponseMetadata": {"RequestId": "req-ns"},
        }
        p.bedrock.converse = MagicMock(return_value=converse_success)
        result = p.handler({"prompt": "Hello"}, None)

        assert result["content"] == "Non-stream result"
        assert "chunks" not in result
        assert "streaming" not in result

    def test_streaming_empty_chunks_on_no_deltas(self):
        """Stream with no contentBlockDelta events yields empty chunks list."""
        p = self._provider()
        events = [
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 0}}},
        ]
        converse_stream_resp = {
            "stream": iter(events),
            "ResponseMetadata": {"RequestId": "req-empty"},
        }
        p.bedrock.converse_stream = MagicMock(return_value=converse_stream_resp)

        with patch.object(p, "apply_guardrail_safe", side_effect=lambda t, *a, **kw: (t, False)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert result["chunks"] == []
        assert result["content"] == ""
        assert result["streaming"] is True


# ---------------------------------------------------------------------------
# Gemini streaming tests
# ---------------------------------------------------------------------------

class TestGeminiStreaming:
    MODULE = "gemini_provider"
    ENV = {
        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        "GUARDRAIL_ID": "test-guardrail",
        "GUARDRAIL_VERSION": "DRAFT",
    }

    def _provider(self, extra_env=None):
        env = {**self.ENV, **(extra_env or {})}
        return _reload_with_env(self.MODULE, env)

    def _no_guardrail(self, t, *a, **kw):
        return t, False

    def _block_output(self, t, gid, gv, source="INPUT"):
        return ("Output blocked.", True) if source == "OUTPUT" else (t, False)

    def _make_sse_lines(self):
        """Build realistic Gemini SSE lines."""
        return [
            'data: {"candidates": [{"content": {"parts": [{"text": "Gemini"}]}, "finishReason": ""}], "usageMetadata": {}}',
            'data: {"candidates": [{"content": {"parts": [{"text": " streaming"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4}, "modelVersion": "gemini-2.5-pro-001"}',
        ]

    def test_streaming_returns_chunks_and_content(self):
        p = self._provider()
        sse_lines = self._make_sse_lines()

        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert EXPECTED_FIELDS.issubset(result.keys())
        assert STREAM_EXTRA_FIELDS.issubset(result.keys())
        assert result["streaming"] is True
        assert result["content"] == "Gemini streaming"
        assert result["chunks"] == ["Gemini", " streaming"]
        assert result["provider"] == "gemini"
        assert result["input_tokens"] == 12
        assert result["output_tokens"] == 4

    def test_streaming_guardrail_applied_to_assembled_text(self):
        p = self._provider()
        sse_lines = self._make_sse_lines()

        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._block_output), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert result["guardrail_blocked"] is True

    def test_streaming_safety_finish_reason_sets_blocked(self):
        p = self._provider()
        sse_lines = [
            'data: {"candidates": [{"content": {"parts": [{"text": "Bad"}]}, "finishReason": "SAFETY"}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1}}',
        ]

        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=_sse_response(sse_lines)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)

        assert result["guardrail_blocked"] is True

    def test_streaming_handles_rate_limit(self):
        p = self._provider()
        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(429)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "Rate limited" in result["error"]

    def test_streaming_handles_auth_error(self):
        p = self._provider()
        with patch.object(p, "_api_key", "bad-key"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=_http_error(403)):
            result = p.handler({"prompt": "Hello", "stream": True}, None)
        assert "invalid" in result["error"].lower() or "not enabled" in result["error"].lower()

    def test_non_streaming_still_works(self):
        p = self._provider()
        api_success = {
            "candidates": [{"content": {"parts": [{"text": "Non-stream"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
            "modelVersion": "gemini-2.5-pro-001",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_success).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = p.handler({"prompt": "Hello"}, None)

        assert result["content"] == "Non-stream"
        assert "chunks" not in result

    def test_streaming_uses_stream_generate_content_url(self):
        """Verify the streaming endpoint URL is used, not generateContent."""
        p = self._provider()
        sse_lines = self._make_sse_lines()
        captured_url = {}

        def capture_urlopen(req, timeout=None):
            captured_url["url"] = req.full_url
            return _sse_response(sse_lines)

        with patch.object(p, "_api_key", "goog-key"), \
             patch.object(p, "apply_guardrail_safe", side_effect=self._no_guardrail), \
             patch("urllib.request.urlopen", side_effect=capture_urlopen):
            p.handler({"prompt": "Hello", "stream": True}, None)

        assert "streamGenerateContent" in captured_url.get("url", "")
        assert "alt=sse" in captured_url.get("url", "")
