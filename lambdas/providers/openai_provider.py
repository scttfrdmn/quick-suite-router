"""
OpenAI Provider — Direct access via Chat Completions API.

Designed for universities and organizations with OpenAI site licenses.
They bring their existing API key; we route through AWS governance.
Auth: API key (+ optional org ID) in Secrets Manager.

Streaming note (v0.6.0):
  When the caller sets stream=True, this provider uses OpenAI's SSE streaming
  Chat Completions API. Because AgentCore Lambda targets are invoked directly
  (not via Lambda function URLs), true SSE push to the caller is not supported.
  Instead, chunks collected during the HTTP stream are returned as a list in
  the `chunks` field alongside the fully assembled `content`. Guardrails are
  applied to the assembled final text. Token counts are derived from chunk
  deltas (accumulated during streaming, since the stream [DONE] event does not
  include usage when streaming).
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
API_URL = "https://api.openai.com/v1/chat/completions"
MAX_CONTEXT_CHARS = 8_000
_creds = None
_creds_fetched_at = 0.0
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
    model = event.get("model", "gpt-4o")
    prompt = event.get("prompt", "")
    system_prompt = event.get("system_prompt", "")
    max_tokens = event.get("max_tokens", 4096)
    temperature = event.get("temperature", 0.7)
    stream = bool(event.get("stream", False))

    if not prompt:
        return _err("No prompt provided")

    creds = _get_creds()
    if not creds or not creds.get("api_key"):
        return _err("OpenAI API key not configured")

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
                "provider": "openai",
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
            "provider": "openai",
            "model": model,
            "guardrail_applied": _guardrail_ok,
            "guardrail_blocked": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "metadata": {},
        }

    # Build messages array — prepend system + history if structured context provided
    if history is not None:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for m in history:
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": prompt})
    else:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {creds['api_key']}",
    }
    if creds.get("organization"):
        headers["OpenAI-Organization"] = creds["organization"]

    if stream:
        return _invoke_streaming(payload, headers, model, _guardrail_ok)
    return _invoke_blocking(payload, headers, model, _guardrail_ok)


def _invoke_blocking(payload, headers, model, guardrail_ok):
    """Standard (non-streaming) OpenAI Chat Completions call."""
    try:
        req = urllib_request.Request(
            API_URL,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=110) as resp:
            data = json.loads(resp.read())

        choices = data.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        if not content and choices:
            logger.warning("OpenAI response has empty or missing content")
        usage = data.get("usage", {})

        # Apply guardrail to output
        content, output_blocked = apply_guardrail_safe(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "provider": "openai",
            "model": model,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "guardrail_applied": guardrail_ok,
            "guardrail_blocked": output_blocked,
            "metadata": {
                "finish_reason": choices[0].get("finish_reason", "") if choices else "",
                "id": data.get("id", ""),
                "actual_model": data.get("model", ""),
            },
        }

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"OpenAI API {e.code}: {body[:300]}")
        if e.code == 429:
            return _err("Rate limited by OpenAI API")
        if e.code == 401:
            return _err("Invalid OpenAI API key or organization")
        return _err(f"OpenAI API error {e.code}")

    except Exception as e:
        logger.error(f"OpenAI invocation failed: {e}")
        return _err(str(e))


def _invoke_streaming(payload, headers, model, guardrail_ok):
    """
    Streaming OpenAI Chat Completions call.

    Parses SSE `data:` lines, handles `[DONE]` terminator, and assembles
    text deltas from `choices[0].delta.content`. Because AgentCore Lambda
    targets cannot push SSE to the caller, all chunks are collected and
    returned in `chunks` alongside fully assembled `content`. Guardrails
    are applied to the assembled final text.
    """
    stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    try:
        req = urllib_request.Request(
            API_URL,
            data=json.dumps(stream_payload).encode(),
            headers=headers,
            method="POST",
        )
        chunks = []
        assembled = []
        input_tokens = 0
        output_tokens = 0
        finish_reason = ""
        resp_id = ""
        actual_model = ""

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

                if not resp_id:
                    resp_id = evt.get("id", "")
                if not actual_model:
                    actual_model = evt.get("model", "")

                # Usage chunk (sent with stream_options.include_usage)
                usage = evt.get("usage")
                if usage:
                    input_tokens = usage.get("prompt_tokens", input_tokens)
                    output_tokens = usage.get("completion_tokens", output_tokens)

                choices = evt.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        chunks.append(text)
                        assembled.append(text)
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr

        content = "".join(assembled)

        # Apply guardrail to fully assembled output
        content, output_blocked = apply_guardrail_safe(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "chunks": chunks,
            "streaming": True,
            "provider": "openai",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "guardrail_applied": guardrail_ok,
            "guardrail_blocked": output_blocked,
            "metadata": {
                "finish_reason": finish_reason,
                "id": resp_id,
                "actual_model": actual_model,
            },
        }

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"OpenAI streaming API {e.code}: {body[:300]}")
        if e.code == 429:
            return _err("Rate limited by OpenAI API")
        if e.code == 401:
            return _err("Invalid OpenAI API key or organization")
        return _err(f"OpenAI API error {e.code}")

    except Exception as e:
        logger.error(f"OpenAI streaming invocation failed: {e}")
        return _err(str(e))


def _get_creds():
    global _creds, _creds_fetched_at
    if _creds and (time.time() - _creds_fetched_at) < _KEY_TTL:
        return _creds
    if not SECRET_ARN:
        return {}
    try:
        resp = secrets.get_secret_value(SecretId=SECRET_ARN)
        _creds = json.loads(resp["SecretString"])
        _creds_fetched_at = time.time()
        return _creds
    except Exception as e:
        logger.error(f"Failed to retrieve OpenAI secret: {e}")
        if _creds:
            _creds_fetched_at = time.time()
            return _creds
        return {}


def _err(msg):
    return {"content": "", "provider": "openai", "model": "", "error": msg,
            "input_tokens": 0, "output_tokens": 0,
            "guardrail_applied": False, "guardrail_blocked": False, "metadata": {}}
