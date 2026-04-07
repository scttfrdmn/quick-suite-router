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

import json
import logging
import os
import re

import boto3
from provider_interface import apply_guardrail, apply_guardrail_safe

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

bedrock = boto3.client("bedrock-runtime")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
_GUARDRAIL_VERSION_SSM_PARAM = os.environ.get("GUARDRAIL_VERSION_SSM_PARAM", "")
_GUARDRAIL_VERSION_FALLBACK = os.environ.get("GUARDRAIL_VERSION", "DRAFT")


def _load_guardrail_version() -> str:
    if _GUARDRAIL_VERSION_SSM_PARAM:
        try:
            ssm = boto3.client("ssm")
            resp = ssm.get_parameter(Name=_GUARDRAIL_VERSION_SSM_PARAM)
            return resp["Parameter"]["Value"]
        except Exception as e:
            logger.warning(f"SSM guardrail version read failed (using env fallback): {e}")
    return _GUARDRAIL_VERSION_FALLBACK


GUARDRAIL_VERSION = _load_guardrail_version()
MAX_CONTEXT_CHARS = 8_000


def _build_extraction_directive(extraction_types: list) -> str:
    """Return a system prompt directive for structured extraction."""
    types_str = ", ".join(f'"{t}"' for t in extraction_types)
    directive = (
        f"Return a JSON object with exactly these top-level keys: {types_str}. "
        "Extract ONLY values explicitly present in the source text. "
        "Do not invent or infer values not stated in the text."
    )
    if "open_problems" in extraction_types:
        directive += (
            ' For "open_problems": extract statements of unresolved questions, '
            'gaps, or future work as a list of objects each with keys '
            '"gap_statement" (string), "domain" (string), "confidence" (float 0.0-1.0).'
        )
    return directive


def _parse_grounding_block(content: str) -> tuple:
    """Extract trailing JSON grounding block from strict-mode research response.

    Returns (cleaned_content, grounding_meta_dict).
    """
    m = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            meta = json.loads(m.group(1))
            cleaned = content[: m.start()].rstrip()
            return cleaned, {
                "sources_used": meta.get("sources_used", []),
                "grounding_coverage": float(meta.get("grounding_coverage", 0.0)),
                "low_confidence_claims": meta.get("low_confidence_claims", []),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    return content, {"sources_used": [], "grounding_coverage": 0.0, "low_confidence_claims": []}


def _parse_extracted_fields(content: str) -> dict:
    """Parse JSON content for extract tool. Falls back to empty dict on failure."""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except (json.JSONDecodeError, TypeError):
                pass
        return {}


def handler(event, context):
    model_id = event.get("model", "anthropic.claude-sonnet-4-20250514-v1:0")
    prompt = event.get("prompt", "")
    system_prompt = event.get("system_prompt", "")
    max_tokens = event.get("max_tokens", 4096)
    temperature = event.get("temperature", 0.7)
    stream = bool(event.get("stream", False))
    tool_name = event.get("tool_name", "")
    extraction_types = event.get("extraction_types") or []
    grounding_mode = event.get("grounding_mode", "default")

    if not prompt:
        return _err("No prompt provided")

    # Inject extraction directive for extract tool
    if tool_name == "extract" and extraction_types:
        directive = _build_extraction_directive(extraction_types)
        system_prompt = directive + ("\n\n" + system_prompt if system_prompt else "")

    # Inject grounding directive for strict research
    if tool_name == "research" and grounding_mode == "strict":
        grounding_directive = (
            "Cite the specific passages that support each claim. "
            "For any claim you cannot ground in the provided text, prefix it with [LOW CONFIDENCE]. "
            "At the end of your response, include a JSON block (nothing after it):\n"
            '```json\n{"sources_used": [...], "grounding_coverage": 0.0, "low_confidence_claims": [...]}\n```'
        )
        system_prompt = grounding_directive + ("\n\n" + system_prompt if system_prompt else "")

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
        result = _invoke_streaming(params, model_id)
    else:
        result = _invoke_blocking(params, model_id)

    if not result.get("error") and not result.get("guardrail_blocked"):
        if tool_name == "extract" and extraction_types:
            result["extracted_fields"] = _parse_extracted_fields(result.get("content", ""))
        elif tool_name == "research" and grounding_mode == "strict":
            cleaned, grounding_meta = _parse_grounding_block(result.get("content", ""))
            result["content"] = cleaned
            result.update(grounding_meta)

    return result


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
