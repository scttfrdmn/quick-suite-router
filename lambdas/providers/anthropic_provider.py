"""
Anthropic Provider — Direct access via the Messages API.

Use when customers want direct Anthropic access (latest models not yet
on Bedrock, organizational agreements, or specific Anthropic features).
Auth: API key in Secrets Manager.

Streaming note (v0.6.0):
  When the caller sets stream=True in the request, this provider uses the
  Anthropic streaming Messages API (SSE). Because AgentCore Lambda targets
  are invoked directly (not via Lambda function URLs), true SSE push to the
  caller is not supported. Instead, chunks collected during the HTTP stream
  are returned as a list in the `chunks` field alongside the fully assembled
  `content` field. The caller can process `chunks` incrementally if desired.
  Guardrails are applied to the fully assembled final text, not individual
  chunks. Token counts are sourced from the message_delta event.
"""

import json
import logging
import os
import time
from urllib import request as urllib_request
from urllib.error import HTTPError

import boto3
from provider_interface import apply_guardrail_safe

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

secrets = boto3.client("secretsmanager")
SECRET_ARN = os.environ.get("SECRET_ARN", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
API_URL = "https://api.anthropic.com/v1/messages"
API_STREAM_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_CONTEXT_CHARS = 8_000
_api_key = None
_api_key_fetched_at = 0.0
_KEY_TTL = 3600


def _parse_context(context_str: str) -> list | None:
    """Return parsed message list if context is structured JSON, else None."""
    if not context_str:
        return None
    try:
        parsed = json.loads(context_str)
        if isinstance(parsed, list) and all("role" in m for m in parsed):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def handler(event, context):
    model = event.get("model", "claude-sonnet-4-20250514")
    prompt = event.get("prompt", "")
    system_prompt = event.get("system_prompt", "")
    max_tokens = event.get("max_tokens", 4096)
    temperature = event.get("temperature", 0.7)
    stream = bool(event.get("stream", False))

    if not prompt:
        return _err("No prompt provided")

    api_key = _get_key()
    if not api_key:
        return _err("Anthropic API key not configured")

    _guardrail_ok = bool(GUARDRAIL_ID)

    ctx = event.get("context", "")
    history = _parse_context(ctx) if ctx else None

    if ctx and history is None:
        # Plain-text context: size check + guardrail
        if len(ctx) > MAX_CONTEXT_CHARS:
            return _err(f"Context exceeds maximum size ({MAX_CONTEXT_CHARS} characters)")
        ctx, ctx_blocked = apply_guardrail_safe(ctx, GUARDRAIL_ID, GUARDRAIL_VERSION, source="INPUT")
        if ctx_blocked:
            return {
                "content": ctx,
                "provider": "anthropic",
                "model": model,
                "guardrail_applied": _guardrail_ok,
                "guardrail_blocked": True,
                "input_tokens": 0,
                "output_tokens": 0,
                "metadata": {},
            }
        prompt = f"Context:\n{ctx}\n\nRequest:\n{prompt}"

    # Apply guardrail to input
    prompt, input_blocked = apply_guardrail_safe(
        prompt, GUARDRAIL_ID, GUARDRAIL_VERSION, source="INPUT"
    )
    if input_blocked:
        return {
            "content": prompt,
            "provider": "anthropic",
            "model": model,
            "guardrail_applied": _guardrail_ok,
            "guardrail_blocked": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "metadata": {},
        }

    # Build messages array — prepend history if structured context provided
    if history is not None:
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": prompt})
    else:
        messages = [{"role": "user", "content": prompt}]

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    }

    if stream:
        return _invoke_streaming(payload, headers, model, _guardrail_ok)
    return _invoke_blocking(payload, headers, model, _guardrail_ok)


def _invoke_blocking(payload, headers, model, guardrail_ok):
    """Standard (non-streaming) Anthropic API call."""
    try:
        req = urllib_request.Request(
            API_URL,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=110) as resp:
            data = json.loads(resp.read())

        content = "".join(
            b.get("text", "") for b in (data.get("content") or [])
            if b.get("type") == "text"
        )
        usage = data.get("usage", {})

        # Apply guardrail to output
        content, output_blocked = apply_guardrail_safe(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "provider": "anthropic",
            "model": model,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "guardrail_applied": guardrail_ok,
            "guardrail_blocked": output_blocked,
            "metadata": {
                "stop_reason": data.get("stop_reason", ""),
                "id": data.get("id", ""),
            },
        }

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Anthropic API {e.code}: {body[:300]}")
        if e.code == 429:
            return _err("Rate limited by Anthropic API")
        if e.code == 401:
            return _err("Invalid Anthropic API key")
        return _err(f"Anthropic API error {e.code}")

    except Exception as e:
        logger.error(f"Anthropic invocation failed: {e}")
        return _err(str(e))


def _invoke_streaming(payload, headers, model, guardrail_ok):
    """
    Streaming Anthropic API call.

    Uses the Messages streaming API (SSE). Handles message_start,
    content_block_delta, and message_delta event types. Because AgentCore
    Lambda targets cannot push SSE to the caller, chunks are collected and
    returned as a list in `chunks` alongside the fully assembled `content`.
    Guardrails are applied to the assembled final text.
    Token counts come from the message_delta usage event.
    """
    stream_payload = {**payload, "stream": True}
    try:
        req = urllib_request.Request(
            API_STREAM_URL,
            data=json.dumps(stream_payload).encode(),
            headers=headers,
            method="POST",
        )
        chunks = []
        assembled = []
        input_tokens = 0
        output_tokens = 0
        stop_reason = ""
        msg_id = ""

        with urllib_request.urlopen(req, timeout=110) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                evt_type = evt.get("type", "")

                if evt_type == "message_start":
                    msg = evt.get("message", {})
                    msg_id = msg.get("id", "")
                    usage = msg.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)

                elif evt_type == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            chunks.append(text)
                            assembled.append(text)

                elif evt_type == "message_delta":
                    delta = evt.get("delta", {})
                    stop_reason = delta.get("stop_reason", stop_reason)
                    usage = evt.get("usage", {})
                    output_tokens = usage.get("output_tokens", output_tokens)

        content = "".join(assembled)

        # Apply guardrail to fully assembled output
        content, output_blocked = apply_guardrail_safe(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "chunks": chunks,
            "streaming": True,
            "provider": "anthropic",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "guardrail_applied": guardrail_ok,
            "guardrail_blocked": output_blocked,
            "metadata": {
                "stop_reason": stop_reason,
                "id": msg_id,
            },
        }

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Anthropic streaming API {e.code}: {body[:300]}")
        if e.code == 429:
            return _err("Rate limited by Anthropic API")
        if e.code == 401:
            return _err("Invalid Anthropic API key")
        return _err(f"Anthropic API error {e.code}")

    except Exception as e:
        logger.error(f"Anthropic streaming invocation failed: {e}")
        return _err(str(e))


def _get_key():
    global _api_key, _api_key_fetched_at
    if _api_key and (time.time() - _api_key_fetched_at) < _KEY_TTL:
        return _api_key
    if not SECRET_ARN:
        return ""
    try:
        resp = secrets.get_secret_value(SecretId=SECRET_ARN)
        _api_key = json.loads(resp["SecretString"]).get("api_key", "")
        _api_key_fetched_at = time.time()
        return _api_key
    except Exception as e:
        logger.error(f"Failed to retrieve Anthropic secret: {e}")
        if _api_key:
            _api_key_fetched_at = time.time()
            return _api_key
        return ""


def _err(msg):
    return {"content": "", "provider": "anthropic", "model": "", "error": msg,
            "input_tokens": 0, "output_tokens": 0,
            "guardrail_applied": False, "guardrail_blocked": False, "metadata": {}}
