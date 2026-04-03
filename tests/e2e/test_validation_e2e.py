"""
E2E validation tests for quick-suite-router.

These tests exercise the router's input validation layer — no LLM calls are made
because the Lambda rejects the request before dispatching to any provider.
Fast and free.
"""

import pytest

from tests.e2e.conftest import call

pytestmark = pytest.mark.e2e


class TestValidationE2E:
    def test_missing_prompt_returns_400(self, api, api_endpoint):
        """Request with no prompt returns HTTP 400."""
        resp = call(api, api_endpoint, "analyze", {})
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
        assert "error" in resp.json()

    def test_empty_prompt_returns_400(self, api, api_endpoint):
        resp = call(api, api_endpoint, "analyze", {"prompt": ""})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_prompt_too_large_returns_400(self, api, api_endpoint):
        """Prompt exceeding 100 KB returns HTTP 400."""
        big = "x" * (100 * 1024 + 1)
        resp = call(api, api_endpoint, "analyze", {"prompt": big})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_temperature_below_zero_returns_400(self, api, api_endpoint):
        resp = call(api, api_endpoint, "analyze", {"prompt": "hi", "temperature": -0.1})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_temperature_above_one_returns_400(self, api, api_endpoint):
        resp = call(api, api_endpoint, "analyze", {"prompt": "hi", "temperature": 1.1})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_max_tokens_zero_returns_400(self, api, api_endpoint):
        resp = call(api, api_endpoint, "analyze", {"prompt": "hi", "max_tokens": 0})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_max_tokens_over_limit_returns_400(self, api, api_endpoint):
        """max_tokens > 16384 returns HTTP 400."""
        resp = call(api, api_endpoint, "analyze", {"prompt": "hi", "max_tokens": 20000})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_unknown_tool_returns_4xx(self, api, api_endpoint):
        """An unknown tool path returns a 4xx (API Gateway 403 or router 400)."""
        resp = call(api, api_endpoint, "nonexistent_tool", {"prompt": "hi"})
        assert resp.status_code in (400, 403, 404), \
            f"Expected 4xx for unknown tool, got {resp.status_code}"
