"""
Gemini Provider — Direct access via Google Generative AI API.

Designed for universities with Google Workspace / AI Enterprise agreements.
Auth: Google AI API key in Secrets Manager.

Streaming note (v0.6.0):
  When the caller sets stream=True, this provider uses the
  `streamGenerateContent` endpoint (SSE). Because AgentCore Lambda targets
  are invoked directly (not via Lambda function URLs), true SSE push to the
  caller is not supported. Instead, chunks collected during the HTTP stream
  are returned as a list in `chunks` alongside the fully assembled `content`.
  Guardrails are applied to the assembled final text. Token counts come from
  the final SSE response chunk's `usageMetadata` field.
"""

import json
import logging
import os
import re
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
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_CONTEXT_CHARS = 8_000
_api_key = None
_api_key_fetched_at = 0.0
_KEY_TTL = 3600


_VALID_ROLES = {"user", "assistant", "system"}
_MAX_HISTORY_MESSAGES = 50
_MAX_MESSAGE_CONTENT_CHARS = 4_000


def _parse_context(context_str: str) -> list | None:
    """Return a validated message list from structured JSON context, else None.

    Validates role values, content type and length, and array bounds to prevent
    prompt injection via crafted chat history (#46).
    """
    if not context_str:
        return None
    try:
        parsed = json.loads(context_str)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    if len(parsed) > _MAX_HISTORY_MESSAGES:
        parsed = parsed[-_MAX_HISTORY_MESSAGES:]  # keep most recent messages
    validated = []
    for m in parsed:
        if not isinstance(m, dict):
            return None  # malformed entry — reject entire history
        role = m.get("role", "")
        content = m.get("content", "")
        if role not in _VALID_ROLES:
            return None  # unknown role — reject
        if not isinstance(content, str) or len(content) > _MAX_MESSAGE_CONTENT_CHARS:
            return None  # non-string or oversized content — reject
        validated.append({"role": role, "content": content})
    return validated if validated else None


def _gemini_role(role: str) -> str:
    """Map OpenAI/Anthropic role names to Gemini role names."""
    return "model" if role == "assistant" else role


def _build_extraction_directive(extraction_types: list) -> str:
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
    model = event.get("model", "gemini-2.5-pro")
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

    api_key = _get_key()
    if not api_key:
        return _err("Gemini API key not configured")

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
                "provider": "gemini",
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
            "provider": "gemini",
            "model": model,
            "guardrail_applied": _guardrail_ok,
            "guardrail_blocked": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "metadata": {},
        }

    # Build contents array — prepend history if structured context provided
    if history is not None:
        contents = [
            {"role": _gemini_role(m["role"]), "parts": [{"text": m["content"]}]}
            for m in history
        ]
        contents.append({"role": "user", "parts": [{"text": prompt}]})
    else:
        contents = [{"role": "user", "parts": [{"text": prompt}]}]

    url = f"{API_BASE}/{model}:generateContent"
    stream_url = f"{API_BASE}/{model}:streamGenerateContent?alt=sse"

    payload = {
        "contents": contents,
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

    if tool_name == "extract" and extraction_types:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    gemini_headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    if stream:
        result = _invoke_streaming(stream_url, payload, gemini_headers, model, _guardrail_ok)
    else:
        result = _invoke_blocking(url, payload, gemini_headers, model, _guardrail_ok)

    if not result.get("error") and not result.get("guardrail_blocked"):
        if tool_name == "extract" and extraction_types:
            result["extracted_fields"] = _parse_extracted_fields(result.get("content", ""))
        elif tool_name == "research" and grounding_mode == "strict":
            cleaned, grounding_meta = _parse_grounding_block(result.get("content", ""))
            result["content"] = cleaned
            result.update(grounding_meta)

    return result


def _invoke_blocking(url, payload, headers, model, guardrail_ok):
    """Standard (non-streaming) Gemini generateContent call."""
    try:
        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
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

        # Apply guardrail to output
        content, output_blocked = apply_guardrail_safe(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "provider": "gemini",
            "model": model,
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
            "guardrail_applied": guardrail_ok,
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


def _invoke_streaming(url, payload, headers, model, guardrail_ok):
    """
    Streaming Gemini streamGenerateContent call (alt=sse).

    Each SSE `data:` line carries a GenerateContentResponse JSON object.
    Text is extracted from candidates[0].content.parts[]. The final chunk
    includes usageMetadata. Because AgentCore Lambda targets cannot push SSE
    to the caller, chunks are collected and returned in `chunks` alongside
    fully assembled `content`. Guardrails are applied to the assembled text.
    """
    try:
        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        chunks = []
        assembled = []
        input_tokens = 0
        output_tokens = 0
        finish_reason = ""
        model_version = ""

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

                if not model_version:
                    model_version = evt.get("modelVersion", "")

                usage = evt.get("usageMetadata", {})
                if usage:
                    input_tokens = usage.get("promptTokenCount", input_tokens)
                    output_tokens = usage.get("candidatesTokenCount", output_tokens)

                candidates = evt.get("candidates", [])
                if candidates:
                    candidate = candidates[0]
                    fr = candidate.get("finishReason", "")
                    if fr:
                        finish_reason = fr
                    for part in candidate.get("content", {}).get("parts", []):
                        text = part.get("text", "")
                        if text:
                            chunks.append(text)
                            assembled.append(text)

        content = "".join(assembled)

        # Apply guardrail to fully assembled output
        content, output_blocked = apply_guardrail_safe(
            content, GUARDRAIL_ID, GUARDRAIL_VERSION, source="OUTPUT"
        )

        return {
            "content": content,
            "chunks": chunks,
            "streaming": True,
            "provider": "gemini",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "guardrail_applied": guardrail_ok,
            "guardrail_blocked": output_blocked or finish_reason == "SAFETY",
            "metadata": {
                "finish_reason": finish_reason,
                "model_version": model_version,
            },
        }

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Gemini streaming API {e.code}: {body[:300]}")
        if e.code == 429:
            return _err("Rate limited by Gemini API")
        if e.code == 403:
            return _err("Gemini API key invalid or model not enabled")
        return _err(f"Gemini API error {e.code}")

    except Exception as e:
        logger.error(f"Gemini streaming invocation failed: {e}")
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
