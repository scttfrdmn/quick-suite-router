"""
Model Router Lambda

Receives task-oriented requests from API Gateway (via AgentCore Gateway),
selects the best available provider, checks the response cache, and
dispatches to the appropriate provider Lambda.

Entry points:
  POST /tools/{tool}  — route to provider
  GET  /status        — show configured providers

Streaming (v0.6.0):
  Callers may set `stream: true` in the request body for the `generate` and
  `research` tool endpoints. The flag is forwarded to the provider Lambda,
  which collects SSE chunks from the upstream LLM API and returns them in a
  `chunks` list alongside the fully assembled `content` field. This "buffered
  streaming" pattern is used because AgentCore Lambda targets are invoked
  directly (not via Lambda function URLs), so true SSE push is not available.
  The full response is still written to the DynamoDB cache on completion.
  Non-streaming callers (stream omitted or false) are unaffected.
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from provider_interface import (
    cache_get,
    cache_key,
    cache_put,
    compute_cost_usd,
    emit_usage_metrics,
    spend_query_department_month,
    spend_record_write,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

lambda_client = boto3.client(
    "lambda",
    config=Config(read_timeout=25, connect_timeout=5),
)
secrets_client = boto3.client("secretsmanager")
_cw_client = None


def _get_cw():
    global _cw_client
    if not _cw_client:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def _emit_event(metric_name: str, tool: str, provider: str = ""):
    """Emit a simple counter event metric to CloudWatch."""
    try:
        dims = [{"Name": "Tool", "Value": tool}]
        if provider:
            dims.append({"Name": "Provider", "Value": provider})
        _get_cw().put_metric_data(
            Namespace="QuickSuiteModelRouter",
            MetricData=[{
                "MetricName": metric_name,
                "Dimensions": dims,
                "Value": 1,
                "Unit": "Count",
            }],
        )
    except Exception as e:
        logger.warning(f"Failed to emit event metric {metric_name}: {e}")

ROUTING_CONFIG = json.loads(os.environ.get("ROUTING_CONFIG", "{}"))
PROVIDER_FUNCTIONS = json.loads(os.environ.get("PROVIDER_FUNCTIONS", "{}"))
PROVIDER_SECRETS = json.loads(os.environ.get("PROVIDER_SECRETS", "{}"))
CACHE_TABLE = os.environ.get("CACHE_TABLE", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL_MINUTES", "60"))
SPEND_TABLE = os.environ.get("SPEND_TABLE", "")
BUDGET_CAPS_SECRET_ARN = os.environ.get("BUDGET_CAPS_SECRET_ARN", "")
# When true, budget cap load failure raises (fail-closed) instead of proceeding (fail-open).
BUDGET_CAPS_REQUIRED = os.environ.get("BUDGET_CAPS_REQUIRED", "").lower() in ("true", "1", "yes")
CORS_ALLOWED_ORIGIN = os.environ.get("CORS_ALLOWED_ORIGIN", "*")
CONTENT_AUDIT_LOGGING = os.environ.get("CONTENT_AUDIT_LOGGING", "").lower() in ("true", "1", "yes")

# Budget caps: loaded once at startup from Secrets Manager (module-level cache).
# Maps department -> monthly_usd_cap. Empty dict = no caps configured.
_budget_caps: dict = {}
_budget_caps_loaded = False


def _load_budget_caps() -> dict:
    """Load budget caps from Secrets Manager.

    Fail-open by default: on error, returns empty caps and resets the loaded
    flag so the next invocation retries. Set BUDGET_CAPS_REQUIRED=true to
    fail-closed (raises, causing Lambda to return a 500).
    """
    global _budget_caps, _budget_caps_loaded
    if _budget_caps_loaded:
        return _budget_caps
    _budget_caps_loaded = True
    if not BUDGET_CAPS_SECRET_ARN:
        return _budget_caps
    try:
        resp = secrets_client.get_secret_value(SecretId=BUDGET_CAPS_SECRET_ARN)
        _budget_caps = json.loads(resp["SecretString"])
        logger.info(json.dumps({"budget_caps_loaded": True, "departments": list(_budget_caps.keys())}))
    except Exception as e:
        _budget_caps_loaded = False  # allow retry on next invocation
        if BUDGET_CAPS_REQUIRED:
            raise RuntimeError("Budget caps are required but failed to load") from e
        logger.warning(f"Budget caps load failed (fail-open): {e}")
    return _budget_caps

_available_providers = None
_available_providers_fetched_at = 0.0
_PROVIDER_CACHE_TTL = 300


def handler(event, context):
    logger.info(json.dumps({
        "tool": event.get("tool"),
        "path": event.get("path"),
        "httpMethod": event.get("httpMethod"),
        "requestId": event.get("requestContext", {}).get("requestId"),
    }, default=str))

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
    # Reject non-JSON Content-Type early (missing header is allowed for direct Lambda invocations)
    headers = event.get("headers") or {}
    ct = headers.get("Content-Type") or headers.get("content-type") or ""
    if ct and "application/json" not in ct.lower():
        return _resp(415, {"error": "Content-Type must be application/json"})

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

    # department/user_id: used for routing overrides, metrics, and spend ledger.
    # Prefer Cognito JWT claims injected by API Gateway authorizer;
    # fall back to body values for direct Lambda invocation (testing / development).
    _claims = (
        event.get("requestContext", {})
             .get("authorizer", {})
             .get("claims", {})
    )
    if _claims:
        user_id = (_claims.get("sub") or _claims.get("cognito:username") or "anonymous")
        department_raw = (_claims.get("custom:department") or "").strip()
    else:
        department_raw = str(body.get("department") or "").strip()
        user_id = str(body.get("user_id") or "anonymous").strip() or "anonymous"
    department = department_raw  # used for routing + metrics (empty = global)
    spend_department = department_raw or "default"

    # Budget cap enforcement
    caps = _load_budget_caps()
    if caps and spend_department in caps:
        cap_usd = float(caps[spend_department])
        try:
            month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
            spent_usd = spend_query_department_month(SPEND_TABLE, spend_department, month_prefix)
            if spent_usd >= cap_usd:
                logger.warning(json.dumps({
                    "budget_exceeded": True,
                    "department": spend_department,
                    "cap_usd": cap_usd,
                    "spent_usd": spent_usd,
                }))
                return _resp(402, {
                    "error": "budget_exceeded",
                    "department": spend_department,
                    "cap_usd": cap_usd,
                    "spent_usd": spent_usd,
                })
        except Exception as e:
            logger.warning(f"Budget check failed (fail-open): {e}")

    # Streaming — only supported for generate and research tools
    _stream_flag = body.get("stream", False)
    stream = (_stream_flag.lower() in ("true", "1", "yes") if isinstance(_stream_flag, str) else bool(_stream_flag))
    STREAMING_TOOLS = {"generate", "research"}
    if stream and tool not in STREAMING_TOOLS:
        stream = False
        logger.info(json.dumps({
            "stream_ignored": True,
            "tool": tool,
            "reason": f"streaming only supported for {sorted(STREAMING_TOOLS)}",
        }))

    # PHI classification: restrict to Bedrock-only when data_classification == "phi"
    data_classification = str(body.get("data_classification") or "").strip().lower()
    phi_mode = data_classification == "phi"

    # Capability requirements
    required_capabilities = body.get("capabilities") or []
    if isinstance(required_capabilities, str):
        required_capabilities = [required_capabilities]

    # Context budget estimate (tokens): prompt + system + context + max_tokens output
    context_budget = (
        estimate_tokens(prompt)
        + estimate_tokens(system_prompt or "")
        + estimate_tokens(str(body.get("context") or ""))
        + max_tokens
    )

    # Select provider
    provider_key, model_id, skip_reason = select_provider(
        tool, body.get("provider"), department,
        phi_mode=phi_mode,
        required_capabilities=required_capabilities,
        context_budget=context_budget,
    )
    if not provider_key:
        if skip_reason == "context_limit_exceeded":
            return _resp(400, {
                "error": "Input exceeds context window of all available providers",
                "code": "context_limit_exceeded",
                "tokens_in_estimate": context_budget,
            })
        if skip_reason == "unsatisfiable_capabilities":
            return _resp(400, {
                "error": f"No providers support required capabilities: {required_capabilities}",
                "code": "unsatisfiable_capabilities",
            })
        return _resp(503, {"error": "No providers available", "tool": tool})

    # Check cache (only for deterministic-ish requests)
    _skip = body.get("skip_cache", False)
    skip_cache = (_skip.lower() in ("true", "1", "yes") if isinstance(_skip, str) else bool(_skip)) or temperature > 0.3
    ck = cache_key(prompt, model_id, system_prompt, max_tokens, body.get("context", ""), temperature, tool)

    if CACHE_TABLE and not skip_cache:
        cached = cache_get(CACHE_TABLE, ck)
        if cached:
            cached["cached"] = True
            emit_usage_metrics(
                provider=cached.get("provider", "cache"),
                model=cached.get("model", ""),
                cache_hit=True,
                department=department,
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
        "stream": stream,
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
            return _fallback(
                tool, provider_key, request_payload, payload, department,
                phi_mode=phi_mode,
                required_capabilities=required_capabilities,
                context_budget=context_budget,
            )

        payload["latency_ms"] = latency
        payload["cached"] = False
        payload["tokens_in_estimate"] = context_budget

        # Emit metrics
        emit_usage_metrics(
            provider=payload.get("provider", provider_key),
            model=payload.get("model", model_id),
            input_tokens=payload.get("input_tokens", 0),
            output_tokens=payload.get("output_tokens", 0),
            latency_ms=latency,
            guardrail_blocked=payload.get("guardrail_blocked", False),
            guardrail_applied=payload.get("guardrail_applied", False),
            cache_hit=False,
            department=department,
        )

        # Write spend record
        spend_record_write(
            table_name=SPEND_TABLE,
            department=spend_department,
            user_id=user_id,
            tool=tool,
            provider=payload.get("provider", provider_key),
            model=payload.get("model", model_id),
            input_tokens=payload.get("input_tokens", 0),
            output_tokens=payload.get("output_tokens", 0),
        )

        # Content audit log (HIPAA audit trail — hashed only, no raw text)
        if CONTENT_AUDIT_LOGGING:
            try:
                logger.info(json.dumps({
                    "audit_log": "content",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "request_id": event.get("requestContext", {}).get("requestId", ""),
                    "tool": tool,
                    "provider": payload.get("provider", provider_key),
                    "model": payload.get("model", model_id),
                    "tokens_in": payload.get("input_tokens", 0),
                    "tokens_out": payload.get("output_tokens", 0),
                    "cost_usd": compute_cost_usd(
                        payload.get("model", model_id),
                        payload.get("input_tokens", 0),
                        payload.get("output_tokens", 0),
                    ),
                    "department": spend_department,
                    "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                    "response_hash": hashlib.sha256(
                        payload.get("content", "").encode()
                    ).hexdigest(),
                }))
            except Exception as _audit_err:
                logger.warning(f"Content audit log failed: {_audit_err}")

        # Cache the response
        if CACHE_TABLE and not skip_cache and not payload.get("error") and not payload.get("guardrail_blocked"):
            cache_put(CACHE_TABLE, ck, payload, ttl_minutes=CACHE_TTL)

        return _resp(200, payload)

    except Exception as e:
        logger.error(f"Failed to invoke {provider_key}: {e}")
        return _fallback(
            tool, provider_key, request_payload, {"error": str(e)}, department,
            phi_mode=phi_mode,
            required_capabilities=required_capabilities,
            context_budget=context_budget,
        )


# ------------------------------------------------------------------
# Provider selection + fallback
# ------------------------------------------------------------------

def _preferred_for(tool: str, department: str = "") -> list:
    """Return the preferred provider list for a tool, respecting department overrides."""
    routing = ROUTING_CONFIG.get("routing", {})
    if department:
        overrides = ROUTING_CONFIG.get("department_overrides", {})
        if department not in overrides:
            logger.warning(json.dumps({
                "unrecognized_department": department,
                "tool": tool,
                "action": "falling_back_to_global_routing",
            }))
        elif tool in overrides[department]:
            return overrides[department][tool].get("preferred", [])
    return routing.get(tool, routing.get("analyze", {})).get("preferred", [])


_NON_BEDROCK_PROVIDERS = {"anthropic", "openai", "gemini"}


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 characters."""
    return max(1, len(text) // 4)


def _get_model_caps(provider_key: str, model_id: str) -> list:
    caps = ROUTING_CONFIG.get("model_capabilities", {})
    return caps.get(f"{provider_key}/{model_id}", [])


def _get_context_window(provider_key: str, model_id: str) -> int:
    windows = ROUTING_CONFIG.get("model_context_windows", {})
    return windows.get(f"{provider_key}/{model_id}", 0)


def select_provider(
    tool: str,
    explicit: str = None,
    department: str = "",
    phi_mode: bool = False,
    required_capabilities: list | None = None,
    context_budget: int = 0,
) -> tuple:
    available = get_available_providers()
    preferred = _preferred_for(tool, department)
    required_capabilities = required_capabilities or []

    # PHI mode: silently restrict candidate set to Bedrock only
    if phi_mode:
        preferred = [e for e in preferred if e.split("/")[0] not in _NON_BEDROCK_PROVIDERS]
        if explicit and explicit in _NON_BEDROCK_PROVIDERS:
            explicit = None  # ignore explicit non-Bedrock request silently

    skip_reason = ""

    if explicit:
        for entry in preferred:
            pk = entry.split("/")[0]
            if pk == explicit and pk in available:
                mid = entry.split("/", 1)[1] if "/" in entry else ""
                caps = _get_model_caps(pk, mid)
                if required_capabilities and not all(c in caps for c in required_capabilities):
                    skip_reason = "unsatisfiable_capabilities"
                    continue
                win = _get_context_window(pk, mid)
                if context_budget > 0 and win > 0 and context_budget > win:
                    skip_reason = "context_limit_exceeded"
                    continue
                return pk, mid, ""

    for entry in preferred:
        pk = entry.split("/")[0]
        mid = entry.split("/", 1)[1] if "/" in entry else ""
        if pk not in available:
            continue
        if required_capabilities:
            caps = _get_model_caps(pk, mid)
            if not all(c in caps for c in required_capabilities):
                if not skip_reason:
                    skip_reason = "unsatisfiable_capabilities"
                continue
        win = _get_context_window(pk, mid)
        if context_budget > 0 and win > 0 and context_budget > win:
            if skip_reason != "unsatisfiable_capabilities":
                skip_reason = "context_limit_exceeded"
            continue
        return pk, mid, ""

    logger.warning(json.dumps({
        "select_provider_exhausted": True,
        "tool": tool,
        "department": department,
        "available": list(available),
        "preferred": preferred,
        "skip_reason": skip_reason,
    }))
    return None, None, skip_reason


def _fallback(
    tool,
    failed,
    request_payload,
    error_payload,
    department: str = "",
    phi_mode: bool = False,
    required_capabilities: list | None = None,
    context_budget: int = 0,
):
    required_capabilities = required_capabilities or []
    preferred = _preferred_for(tool, department)
    if phi_mode:
        preferred = [e for e in preferred if e.split("/")[0] not in _NON_BEDROCK_PROVIDERS]
    available = get_available_providers()

    past_failed = False
    for entry in preferred:
        pk = entry.split("/")[0]
        mid = entry.split("/", 1)[1] if "/" in entry else ""

        if pk == failed:
            past_failed = True
            continue

        if not past_failed or pk not in available:
            continue

        if required_capabilities:
            caps = _get_model_caps(pk, mid)
            if not all(c in caps for c in required_capabilities):
                continue
        win = _get_context_window(pk, mid)
        if context_budget > 0 and win > 0 and context_budget > win:
            continue

        fn_arn = PROVIDER_FUNCTIONS.get(pk)
        if not fn_arn:
            continue
        try:
            logger.info(f"Falling back → {pk}/{mid}")
            _emit_event("FallbackInvoked", tool, failed)
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
                    guardrail_applied=payload.get("guardrail_applied", False),
                    cache_hit=False,
                    department=department,
                )
                return _resp(200, payload)
        except Exception as e:
            logger.error(f"Fallback to {pk} also failed: {e}")

    _emit_event("AllProvidersFailed", tool)
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
            "Access-Control-Allow-Origin": CORS_ALLOWED_ORIGIN,
        },
        "body": json.dumps(body, default=str),
    }
