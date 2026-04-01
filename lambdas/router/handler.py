"""
Model Router Lambda

Receives task-oriented requests from API Gateway (via AgentCore Gateway),
selects the best available provider, checks the response cache, and
dispatches to the appropriate provider Lambda.

Entry points:
  POST /tools/{tool}  — route to provider
  GET  /status        — show configured providers
"""

import json
import logging
import os
import time

import boto3
from botocore.config import Config
from provider_interface import (
    cache_get,
    cache_key,
    cache_put,
    emit_usage_metrics,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

lambda_client = boto3.client(
    "lambda",
    config=Config(read_timeout=25, connect_timeout=5),
)
secrets_client = boto3.client("secretsmanager")

ROUTING_CONFIG = json.loads(os.environ.get("ROUTING_CONFIG", "{}"))
PROVIDER_FUNCTIONS = json.loads(os.environ.get("PROVIDER_FUNCTIONS", "{}"))
PROVIDER_SECRETS = json.loads(os.environ.get("PROVIDER_SECRETS", "{}"))
CACHE_TABLE = os.environ.get("CACHE_TABLE", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL_MINUTES", "60"))

_available_providers = None
_available_providers_fetched_at = 0.0
_PROVIDER_CACHE_TTL = 300


def handler(event, context):
    logger.info(json.dumps(event, default=str))

    http_method = event.get("httpMethod", "POST")
    if http_method == "GET" or event.get("resource") == "/status":
        return handle_status()

    return handle_tool_invocation(event)


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

def handle_status():
    available = get_available_providers()
    routing = ROUTING_CONFIG.get("routing", {})
    status = {
        "providers": {"bedrock": {"available": True, "auth": "IAM"}},
        "tools": list(routing.keys()),
        "cache_enabled": bool(CACHE_TABLE),
    }
    for p in ["anthropic", "openai", "gemini"]:
        status["providers"][p] = {
            "available": p in available,
            "auth": "API key in Secrets Manager",
        }
    return _resp(200, status)


# ------------------------------------------------------------------
# Tool invocation
# ------------------------------------------------------------------

def handle_tool_invocation(event):
    tool = event.get("tool")
    if not tool:
        path = event.get("path", "")
        parts = path.strip("/").split("/")
        tool = parts[-1] if parts else None
    if not tool:
        return _resp(400, {"error": "No tool specified"})

    body_str = event.get("body", "{}")
    try:
        body = json.loads(body_str) if isinstance(body_str, str) else (body_str or {})
    except (json.JSONDecodeError, TypeError):
        return _resp(400, {"error": "Invalid request body: expected JSON"})

    prompt = body.get("prompt", body.get("input", ""))
    if not prompt:
        return _resp(400, {"error": "No prompt provided"})

    MAX_PROMPT_BYTES = 100_000
    if len(prompt.encode()) > MAX_PROMPT_BYTES:
        return _resp(400, {"error": f"Prompt exceeds maximum size ({MAX_PROMPT_BYTES} bytes)"})

    defaults = ROUTING_CONFIG.get("defaults", {})

    MAX_OUTPUT_TOKENS = 16_384
    max_tokens = body.get("max_tokens", defaults.get("max_tokens", 4096))
    if not isinstance(max_tokens, int) or not (1 <= max_tokens <= MAX_OUTPUT_TOKENS):
        return _resp(400, {"error": f"max_tokens must be 1–{MAX_OUTPUT_TOKENS}"})

    temperature = body.get("temperature", defaults.get("temperature", 0.7))
    if not isinstance(temperature, (int, float)) or not (0.0 <= temperature <= 1.0):
        return _resp(400, {"error": "temperature must be between 0.0 and 1.0"})

    system_prompt = _system_prompt(tool)

    # Select provider
    provider_key, model_id = select_provider(tool, body.get("provider"))
    if not provider_key:
        return _resp(503, {"error": "No providers available", "tool": tool})

    # Check cache (only for deterministic-ish requests)
    _skip = body.get("skip_cache", False)
    skip_cache = (_skip.lower() in ("true", "1", "yes") if isinstance(_skip, str) else bool(_skip)) or temperature > 0.3
    ck = cache_key(prompt, model_id, system_prompt, max_tokens, body.get("context", ""), temperature)

    if CACHE_TABLE and not skip_cache:
        cached = cache_get(CACHE_TABLE, ck)
        if cached:
            cached["cached"] = True
            emit_usage_metrics(
                provider=cached.get("provider", "cache"),
                model=cached.get("model", ""),
                cache_hit=True,
            )
            logger.info(f"Cache hit for tool={tool}")
            return _resp(200, cached)

    # Build request
    request_payload = {
        "prompt": prompt,
        "system_prompt": system_prompt,
        "model": model_id,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "tool_name": tool,
        "context": body.get("context", ""),
    }

    # Invoke provider
    fn_arn = PROVIDER_FUNCTIONS.get(provider_key)
    if not fn_arn:
        return _resp(500, {"error": f"No Lambda for provider: {provider_key}"})

    try:
        logger.info(f"Routing tool={tool} → {provider_key}/{model_id}")
        start = time.time()

        resp = lambda_client.invoke(
            FunctionName=fn_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(request_payload),
        )
        payload = json.loads(resp["Payload"].read())
        latency = int((time.time() - start) * 1000)

        if "errorMessage" in payload or payload.get("error"):
            logger.error(f"Provider error: {payload.get('errorMessage') or payload.get('error')}")
            return _fallback(tool, provider_key, request_payload, payload)

        payload["latency_ms"] = latency
        payload["cached"] = False

        # Emit metrics
        emit_usage_metrics(
            provider=payload.get("provider", provider_key),
            model=payload.get("model", model_id),
            input_tokens=payload.get("input_tokens", 0),
            output_tokens=payload.get("output_tokens", 0),
            latency_ms=latency,
            guardrail_blocked=payload.get("guardrail_blocked", False),
            cache_hit=False,
        )

        # Cache the response
        if CACHE_TABLE and not skip_cache and not payload.get("error") and not payload.get("guardrail_blocked"):
            cache_put(CACHE_TABLE, ck, payload, ttl_minutes=CACHE_TTL)

        return _resp(200, payload)

    except Exception as e:
        logger.error(f"Failed to invoke {provider_key}: {e}")
        return _fallback(
            tool, provider_key, request_payload, {"error": str(e)}
        )


# ------------------------------------------------------------------
# Provider selection + fallback
# ------------------------------------------------------------------

def select_provider(tool: str, explicit: str = None) -> tuple:
    available = get_available_providers()
    routing = ROUTING_CONFIG.get("routing", {})
    tool_cfg = routing.get(tool, routing.get("analyze", {}))
    preferred = tool_cfg.get("preferred", [])

    if explicit:
        for entry in preferred:
            pk = entry.split("/")[0]
            if pk == explicit and pk in available:
                mid = entry.split("/", 1)[1] if "/" in entry else ""
                return pk, mid

    for entry in preferred:
        pk = entry.split("/")[0]
        mid = entry.split("/", 1)[1] if "/" in entry else ""
        if pk in available:
            return pk, mid

    logger.warning(json.dumps({
        "select_provider_exhausted": True,
        "tool": tool,
        "available": list(available),
        "preferred": preferred,
    }))
    return None, None


def _fallback(tool, failed, request_payload, error_payload):
    routing = ROUTING_CONFIG.get("routing", {})
    preferred = routing.get(tool, {}).get("preferred", [])
    available = get_available_providers()

    past_failed = False
    for entry in preferred:
        pk = entry.split("/")[0]
        mid = entry.split("/", 1)[1] if "/" in entry else ""

        if pk == failed:
            past_failed = True
            continue

        if past_failed and pk in available:
            fn_arn = PROVIDER_FUNCTIONS.get(pk)
            if not fn_arn:
                continue
            try:
                logger.info(f"Falling back → {pk}/{mid}")
                request_payload["model"] = mid
                fb_start = time.time()
                resp = lambda_client.invoke(
                    FunctionName=fn_arn,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(request_payload),
                )
                payload = json.loads(resp["Payload"].read())
                fb_latency = int((time.time() - fb_start) * 1000)
                if "errorMessage" not in payload and not payload.get("error"):
                    payload["latency_ms"] = fb_latency
                    payload["_fallback"] = {
                        "original_provider": failed,
                        "reason": str(error_payload.get("error", "unknown")),
                    }
                    emit_usage_metrics(
                        provider=payload.get("provider", pk),
                        model=payload.get("model", mid),
                        input_tokens=payload.get("input_tokens", 0),
                        output_tokens=payload.get("output_tokens", 0),
                        latency_ms=fb_latency,
                        guardrail_blocked=payload.get("guardrail_blocked", False),
                        cache_hit=False,
                    )
                    return _resp(200, payload)
            except Exception as e:
                logger.error(f"Fallback to {pk} also failed: {e}")

    last_error_msg = (
        error_payload.get("error", "unknown")
        if isinstance(error_payload, dict)
        else str(error_payload)
    )
    return _resp(503, {
        "content": "", "provider": "none", "model": "",
        "error": "All providers failed", "input_tokens": 0, "output_tokens": 0,
        "guardrail_applied": False, "guardrail_blocked": False, "metadata": {},
        "tool": tool, "last_error": last_error_msg,
    })


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def get_available_providers() -> set:
    global _available_providers, _available_providers_fetched_at
    if _available_providers is not None and \
       (time.time() - _available_providers_fetched_at) < _PROVIDER_CACHE_TTL:
        return _available_providers

    available = {"bedrock"}
    had_errors = False
    for provider, secret_arn in PROVIDER_SECRETS.items():
        try:
            resp = secrets_client.get_secret_value(SecretId=secret_arn)
            data = json.loads(resp["SecretString"])
            if data.get("api_key"):
                available.add(provider)
                logger.info(f"Provider {provider}: configured")
        except secrets_client.exceptions.ResourceNotFoundException:
            logger.info(f"Provider {provider}: not configured")
        except Exception as e:
            logger.warning(f"Provider {provider}: error: {e}")
            had_errors = True

    if had_errors and _available_providers is not None:
        # Preserve stale cache when Secrets Manager is transiently unreachable —
        # avoids dropping providers that were working before the failure.
        _available_providers_fetched_at = time.time()
        return _available_providers

    _available_providers = available
    _available_providers_fetched_at = time.time()
    return available


def _system_prompt(tool: str) -> str:
    routing = ROUTING_CONFIG.get("routing", {})
    return routing.get(tool, {}).get(
        "system_prompt", "You are a helpful assistant."
    )


def _resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }
