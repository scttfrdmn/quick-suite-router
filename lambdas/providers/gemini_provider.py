"""
Gemini Provider — Direct access via Google Generative AI API.

Designed for universities with Google Workspace / AI Enterprise agreements.
Auth: Google AI API key in Secrets Manager.
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
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_CONTEXT_CHARS = 8_000
_api_key = None
_api_key_fetched_at = 0.0
_KEY_TTL = 3600


def handler(event, context):
    model = event.get("model", "gemini-2.5-pro")
    prompt = event.get("prompt", "")
    system_prompt = event.get("system_prompt", "")
    max_tokens = event.get("max_tokens", 4096)
    temperature = event.get("temperature", 0.7)

    if not prompt:
        return _err("No prompt provided")

    api_key = _get_key()
    if not api_key:
        return _err("Gemini API key not configured")

    ctx = event.get("context", "")
    if ctx:
        if len(ctx) > MAX_CONTEXT_CHARS:
            return _err(f"Context exceeds maximum size ({MAX_CONTEXT_CHARS} characters)")
        ctx, ctx_blocked = apply_guardrail(ctx, GUARDRAIL_ID, GUARDRAIL_VERSION, source="INPUT")
        if ctx_blocked:
            return {
                "content": ctx,
                "provider": "gemini",
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
            "provider": "gemini",
            "model": model,
            "guardrail_applied": True,
            "guardrail_blocked": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "metadata": {},
        }

    url = f"{API_BASE}/{model}:generateContent"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
        # Permissive safety — we have Bedrock Guardrails upstream
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
            for c in [
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            ]
        ],
    }

    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    try:
        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=110) as resp:
            data = json.loads(resp.read())

        candidates = data.get("candidates", [])
        content = ""
        if candidates:
            for part in candidates[0].get("content", {}).get("parts", []):
                if "text" in part:
                    content += part["text"]

        usage = data.get("usageMetadata", {})
        finish = candidates[0].get("finishReason", "") if candidates else ""

        # Apply guardrail to output (TODO §1)
        content, output_blocked = apply_guardrail(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "provider": "gemini",
            "model": model,
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
            "guardrail_applied": bool(GUARDRAIL_ID),
            "guardrail_blocked": output_blocked or finish == "SAFETY",
            "metadata": {
                "finish_reason": finish,
                "model_version": data.get("modelVersion", ""),
            },
        }

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Gemini API {e.code}: {body[:300]}")
        if e.code == 429:
            return _err("Rate limited by Gemini API")
        if e.code == 403:
            return _err("Gemini API key invalid or model not enabled")
        return _err(f"Gemini API error {e.code}")

    except Exception as e:
        logger.error(f"Gemini invocation failed: {e}")
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
        logger.error(f"Failed to retrieve Gemini secret: {e}")
        if _api_key:
            _api_key_fetched_at = time.time()
            return _api_key
        return ""


def _err(msg):
    return {"content": "", "provider": "gemini", "model": "", "error": msg,
            "input_tokens": 0, "output_tokens": 0,
            "guardrail_applied": False, "guardrail_blocked": False, "metadata": {}}
