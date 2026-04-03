"""
E2E tests for the five tool endpoints against the deployed QuickSuiteModelRouter stack.

Uses minimal prompts (low max_tokens, temperature=0) to keep cost and latency low.
All tools route to Bedrock by default — no external provider secrets required.
"""

import pytest

from tests.e2e.conftest import call

pytestmark = pytest.mark.e2e

# Minimal payload used across tools to keep Bedrock cost and latency low
_FAST = {"temperature": 0.0, "max_tokens": 16}


class TestToolsE2E:
    def test_analyze_returns_200(self, api, api_endpoint):
        """POST /tools/analyze returns 200 with content."""
        resp = call(api, api_endpoint, "analyze",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200, f"Unexpected status: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "content" in body, f"Missing content: {body}"
        assert body["content"], "Empty content"

    def test_generate_returns_200(self, api, api_endpoint):
        resp = call(api, api_endpoint, "generate",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200, f"Unexpected status: {resp.status_code} {resp.text}"
        assert resp.json().get("content")

    def test_research_returns_200(self, api, api_endpoint):
        resp = call(api, api_endpoint, "research",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200, f"Unexpected status: {resp.status_code} {resp.text}"
        assert resp.json().get("content")

    def test_summarize_returns_200(self, api, api_endpoint):
        resp = call(api, api_endpoint, "summarize",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200, f"Unexpected status: {resp.status_code} {resp.text}"
        assert resp.json().get("content")

    def test_code_returns_200(self, api, api_endpoint):
        resp = call(api, api_endpoint, "code",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200, f"Unexpected status: {resp.status_code} {resp.text}"
        assert resp.json().get("content")

    def test_response_has_required_fields(self, api, api_endpoint):
        """Every successful response includes the standard field set."""
        resp = call(api, api_endpoint, "analyze",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200
        body = resp.json()
        for field in ("content", "provider", "model", "input_tokens", "output_tokens",
                      "latency_ms", "guardrail_applied"):
            assert field in body, f"Missing field '{field}' in response: {body}"

    def test_default_provider_is_bedrock(self, api, api_endpoint):
        """Without an explicit provider override, the router uses Bedrock."""
        resp = call(api, api_endpoint, "analyze",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200
        body = resp.json()
        # Bedrock is always the default — other providers need secrets populated
        assert body.get("provider") == "bedrock", \
            f"Expected bedrock provider, got: {body.get('provider')}"

    def test_explicit_bedrock_provider_honored(self, api, api_endpoint):
        """Explicit provider=bedrock is honored."""
        resp = call(api, api_endpoint, "analyze",
                    {"prompt": "Reply with only the word 'ok'.", "provider": "bedrock", **_FAST})
        assert resp.status_code == 200
        assert resp.json().get("provider") == "bedrock"

    def test_token_counts_are_positive(self, api, api_endpoint):
        """input_tokens and output_tokens are positive integers."""
        resp = call(api, api_endpoint, "summarize",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("input_tokens", 0) > 0
        assert body.get("output_tokens", 0) > 0

    def test_latency_ms_is_positive(self, api, api_endpoint):
        resp = call(api, api_endpoint, "code",
                    {"prompt": "What is 2 + 2?", **_FAST})
        assert resp.status_code == 200
        assert resp.json().get("latency_ms", 0) > 0

    def test_guardrail_not_blocked_on_benign_prompt(self, api, api_endpoint):
        """A safe prompt is not blocked by Bedrock Guardrails."""
        resp = call(api, api_endpoint, "analyze",
                    {"prompt": "What is the capital of France?", **_FAST})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("guardrail_blocked") is not True, \
            f"Safe prompt was blocked by guardrail: {body}"
