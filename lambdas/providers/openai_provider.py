"""
OpenAI Provider — Direct access via Chat Completions API.

Designed for universities and organizations with OpenAI site licenses.
They bring their existing API key; we route through AWS governance.
Auth: API key (+ optional org ID) in Secrets Manager.
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
            "guardrail_applied": _guardrail_ok,
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
