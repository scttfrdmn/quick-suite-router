"""
Bedrock Provider — Invokes foundation models via the Converse API.

Supports Claude, Nova, Llama, Mistral, and any Bedrock-hosted model.
Uses Converse API for unified interface across model families.
Auth: IAM (no external credentials needed).
"""

import logging
import os

import boto3
from provider_interface import apply_guardrail

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

bedrock = boto3.client("bedrock-runtime")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
MAX_CONTEXT_CHARS = 8_000


def handler(event, context):
    model_id = event.get("model", "anthropic.claude-sonnet-4-20250514-v1:0")
    prompt = event.get("prompt", "")
    system_prompt = event.get("system_prompt", "")
    max_tokens = event.get("max_tokens", 4096)
    temperature = event.get("temperature", 0.7)

    if not prompt:
        return _err("No prompt provided")

    ctx = event.get("context", "")
    if ctx:
        if len(ctx) > MAX_CONTEXT_CHARS:
            return _err(f"Context exceeds maximum size ({MAX_CONTEXT_CHARS} characters)")
        ctx, ctx_blocked = apply_guardrail(ctx, GUARDRAIL_ID, GUARDRAIL_VERSION, source="INPUT")
        if ctx_blocked:
            return {
                "content": ctx,
                "provider": "bedrock",
                "model": model_id,
                "guardrail_applied": True,
                "guardrail_blocked": True,
                "input_tokens": 0,
                "output_tokens": 0,
                "metadata": {},
            }
        prompt = f"Context:\n{ctx}\n\nRequest:\n{prompt}"

    params = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }

    if system_prompt:
        params["system"] = [{"text": system_prompt}]

    if GUARDRAIL_ID:
        params["guardrailConfig"] = {
            "guardrailIdentifier": GUARDRAIL_ID,
            "guardrailVersion": GUARDRAIL_VERSION,
        }

    try:
        resp = bedrock.converse(**params)

        content = ""
        for block in resp.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                content += block["text"]

        usage = resp.get("usage", {})
        stop = resp.get("stopReason", "end_turn")

        return {
            "content": content,
            "provider": "bedrock",
            "model": model_id,
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "guardrail_applied": bool(GUARDRAIL_ID),
            "guardrail_blocked": stop == "guardrail_intervened",
            "metadata": {
                "stop_reason": stop,
                "request_id": resp.get("ResponseMetadata", {}).get("RequestId", ""),
            },
        }

    except bedrock.exceptions.ThrottlingException as e:
        return _err(f"Rate limited: {e}")
    except bedrock.exceptions.ModelNotReadyException as e:
        return _err(f"Model not available: {e}")
    except bedrock.exceptions.ValidationException as e:
        return _err(f"Invalid request: {e}")
    except Exception as e:
        logger.error(f"Bedrock invocation failed: {e}")
        return _err(str(e))


def _err(msg):
    return {"content": "", "provider": "bedrock", "model": "", "error": msg,
            "input_tokens": 0, "output_tokens": 0,
            "guardrail_applied": False, "guardrail_blocked": False, "metadata": {}}
