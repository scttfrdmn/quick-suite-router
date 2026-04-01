"""
Common provider interface and governance utilities.

Every provider Lambda implements the same contract:
  Input:  dict with prompt, model, system_prompt, max_tokens, temperature
  Output: dict with content, provider, model, input_tokens, output_tokens, etc.

Governance functions wrap all provider calls with Bedrock Guardrails
and CloudWatch usage metering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

_bedrock_client = None
_cw_client = None
_dynamo_resource = None


def get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime")
    return _bedrock_client


def get_cw_client():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def get_dynamo_table(table_name: str):
    global _dynamo_resource
    if _dynamo_resource is None:
        _dynamo_resource = boto3.resource("dynamodb")
    return _dynamo_resource.Table(table_name)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def apply_guardrail(
    text: str,
    guardrail_id: str,
    guardrail_version: str = "DRAFT",
    source: str = "INPUT",
) -> tuple[str, bool]:
    """
    Apply Bedrock Guardrail to text.
    Returns (processed_text, was_blocked).
    """
    if not guardrail_id:
        return text, False

    try:
        client = get_bedrock_client()
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source=source,
            content=[{"text": {"text": text}}],
        )

        action = response.get("action", "NONE")
        if action == "GUARDRAIL_INTERVENED":
            outputs = response.get("outputs", [])
            blocked_text = (
                outputs[0]["text"] if outputs else "Content blocked by policy."
            )
            return blocked_text, True

        return text, False

    except Exception as e:
        logger.warning(f"Guardrail application failed (fail-open): {e}")
        return text, False


# ---------------------------------------------------------------------------
# Usage metering
# ---------------------------------------------------------------------------

def emit_usage_metrics(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    guardrail_blocked: bool = False,
    cache_hit: bool = False,
):
    """Emit CloudWatch custom metrics for usage tracking."""
    try:
        client = get_cw_client()
        dimensions = [
            {"Name": "Provider", "Value": provider},
            {"Name": "Model", "Value": model or "unknown"},
        ]

        metrics = [
            {
                "MetricName": "InputTokens",
                "Dimensions": dimensions,
                "Value": input_tokens,
                "Unit": "Count",
            },
            {
                "MetricName": "OutputTokens",
                "Dimensions": dimensions,
                "Value": output_tokens,
                "Unit": "Count",
            },
            {
                "MetricName": "Latency",
                "Dimensions": dimensions,
                "Value": latency_ms,
                "Unit": "Milliseconds",
            },
        ]

        if guardrail_blocked:
            metrics.append({
                "MetricName": "GuardrailBlocked",
                "Dimensions": dimensions,
                "Value": 1,
                "Unit": "Count",
            })

        if cache_hit:
            metrics.append({
                "MetricName": "CacheHit",
                "Dimensions": [{"Name": "Provider", "Value": "cache"}],
                "Value": 1,
                "Unit": "Count",
            })
        else:
            metrics.append({
                "MetricName": "CacheMiss",
                "Dimensions": [{"Name": "Provider", "Value": "cache"}],
                "Value": 1,
                "Unit": "Count",
            })

        client.put_metric_data(
            Namespace="QuickSuiteModelRouter",
            MetricData=metrics,
        )
    except Exception as e:
        logger.warning(f"Failed to emit metrics: {e}")


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------

def cache_key(prompt: str, model: str, system_prompt: str = "",
              max_tokens: int = 4096, context: str = "",
              temperature: float = 0.0) -> str:
    """Generate a deterministic cache key from request parameters."""
    raw = f"{model}|{system_prompt}|{max_tokens}|{temperature}|{context}|{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_get(table_name: str, key: str) -> Optional[dict]:
    """Retrieve a cached response. Returns None on miss or error."""
    if not table_name:
        return None
    try:
        table = get_dynamo_table(table_name)
        resp = table.get_item(Key={"cache_key": key})
        item = resp.get("Item")
        if item and "response" in item:
            return json.loads(item["response"])
        return None
    except Exception as e:
        logger.warning(f"Cache read failed: {e}")
        return None


def cache_put(
    table_name: str,
    key: str,
    response: dict,
    ttl_minutes: int = 60,
):
    """Store a response in the cache with TTL."""
    if not table_name:
        return
    try:
        table = get_dynamo_table(table_name)
        table.put_item(
            Item={
                "cache_key": key,
                "response": json.dumps(response),
                "ttl": int(time.time()) + (ttl_minutes * 60),
            }
        )
    except Exception as e:
        logger.warning(f"Cache write failed: {e}")
