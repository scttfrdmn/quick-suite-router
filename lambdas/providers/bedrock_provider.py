"""
Bedrock Provider — Invokes foundation models via the Converse API.

Supports Claude, Nova, Llama, Mistral, and any Bedrock-hosted model.
Uses Converse API for unified interface across model families.
Auth: IAM (no external credentials needed).

Streaming note (v0.6.0):
  When the caller sets stream=True, this provider uses `converse_stream()`
  instead of `converse()`. The event stream is consumed and `contentBlockDelta`
  events are mapped to text chunks. Because AgentCore Lambda targets cannot push
  SSE to the caller, chunks are collected and returned in `chunks` alongside the
  fully assembled `content`. Guardrails are applied to the assembled final text,
  not individual chunks (matching the `converse()` guardrail contract).
"""

import logging
import os

import boto3
from provider_interface import apply_guardrail, apply_guardrail_safe

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
    stream = bool(event.get("stream", False))

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

    if stream:
        return _invoke_streaming(params, model_id)
    return _invoke_blocking(params, model_id)


def _invoke_blocking(params, model_id):
    """Standard (non-streaming) Bedrock Converse call."""
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


def _invoke_streaming(params, model_id):
    """
    Streaming Bedrock Converse call using converse_stream().

    Consumes the event stream and maps `contentBlockDelta` events to text
    chunks. Because AgentCore Lambda targets cannot push SSE to the caller,
    chunks are collected and returned in `chunks` alongside fully assembled
    `content`. Guardrails are applied to the assembled final text (not
    individual chunks) via apply_guardrail — matching the non-streaming
    guardrail contract.
    """
    try:
        resp = bedrock.converse_stream(**params)
        stream = resp.get("stream")

        chunks = []
        assembled = []
        input_tokens = 0
        output_tokens = 0
        stop_reason = "end_turn"
        request_id = resp.get("ResponseMetadata", {}).get("RequestId", "")
        guardrail_blocked = False

        for event in stream:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                text = delta.get("text", "")
                if text:
                    chunks.append(text)
                    assembled.append(text)

            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason", stop_reason)
                if stop_reason == "guardrail_intervened":
                    guardrail_blocked = True

            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                input_tokens = usage.get("inputTokens", input_tokens)
                output_tokens = usage.get("outputTokens", output_tokens)

        content = "".join(assembled)

        # Apply guardrail to fully assembled output (guardrailConfig on converse_stream
        # handles content filtering at the service side; apply_guardrail_safe here
        # provides the same programmatic check as the non-streaming path).
        if GUARDRAIL_ID and not guardrail_blocked:
            content, programmatic_blocked = apply_guardrail_safe(
                content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
            )
            guardrail_blocked = programmatic_blocked

        return {
            "content": content,
            "chunks": chunks,
            "streaming": True,
            "provider": "bedrock",
            "model": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "guardrail_applied": bool(GUARDRAIL_ID),
            "guardrail_blocked": guardrail_blocked,
            "metadata": {
                "stop_reason": stop_reason,
                "request_id": request_id,
            },
        }

    except bedrock.exceptions.ThrottlingException as e:
        return _err(f"Rate limited: {e}")
    except bedrock.exceptions.ModelNotReadyException as e:
        return _err(f"Model not available: {e}")
    except bedrock.exceptions.ValidationException as e:
        return _err(f"Invalid request: {e}")
    except Exception as e:
        logger.error(f"Bedrock streaming invocation failed: {e}")
        return _err(str(e))


def _err(msg):
    return {"content": "", "provider": "bedrock", "model": "", "error": msg,
            "input_tokens": 0, "output_tokens": 0,
            "guardrail_applied": False, "guardrail_blocked": False, "metadata": {}}
