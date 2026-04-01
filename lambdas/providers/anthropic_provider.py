"""
Anthropic Provider — Direct access via the Messages API.

Use when customers want direct Anthropic access (latest models not yet
on Bedrock, organizational agreements, or specific Anthropic features).
Auth: API key in Secrets Manager.
"""

import json
import logging
import os
import time
from urllib import request as urllib_request
from urllib.error import HTTPError

import boto3
from provider_interface import apply_guardrail

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

secrets = boto3.client("secretsmanager")
SECRET_ARN = os.environ.get("SECRET_ARN", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_CONTEXT_CHARS = 8_000
_api_key = None
_api_key_fetched_at = 0.0
_KEY_TTL = 3600


def handler(event, context):
    model = event.get("model", "claude-sonnet-4-20250514")
    prompt = event.get("prompt", "")
    system_prompt = event.get("system_prompt", "")
    max_tokens = event.get("max_tokens", 4096)
    temperature = event.get("temperature", 0.7)

    if not prompt:
        return _err("No prompt provided")

    api_key = _get_key()
    if not api_key:
        return _err("Anthropic API key not configured")

    ctx = event.get("context", "")
    if ctx:
        if len(ctx) > MAX_CONTEXT_CHARS:
            return _err(f"Context exceeds maximum size ({MAX_CONTEXT_CHARS} characters)")
        ctx, ctx_blocked = apply_guardrail(ctx, GUARDRAIL_ID, GUARDRAIL_VERSION, source="INPUT")
        if ctx_blocked:
            return {
                "content": ctx,
                "provider": "anthropic",
                "model": model,
                "guardrail_applied": True,
                "guardrail_blocked": True,
                "input_tokens": 0,
                "output_tokens": 0,
                "metadata": {},
            }
        prompt = f"Context:\n{ctx}\n\nRequest:\n{prompt}"

    # Apply guardrail to input
    prompt, input_blocked = apply_guardrail(
        prompt, GUARDRAIL_ID, GUARDRAIL_VERSION, source="INPUT"
    )
    if input_blocked:
        return {
            "content": prompt,
            "provider": "anthropic",
            "model": model,
            "guardrail_applied": True,
            "guardrail_blocked": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "metadata": {},
        }

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    }

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

        # Apply guardrail to output (TODO §1)
        content, output_blocked = apply_guardrail(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "provider": "anthropic",
            "model": model,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "guardrail_applied": bool(GUARDRAIL_ID),
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
