"""
E2E tests for /health and /status endpoints.

/health is unauthenticated — tests run without the Bearer token.
/status requires auth and returns available providers.
"""

import pytest
import requests

from tests.e2e.conftest import call

pytestmark = pytest.mark.e2e


class TestHealthStatusE2E:
    def test_health_unauthenticated(self, api_endpoint):
        """GET /health returns 200 without an Authorization header."""
        resp = requests.get(f"{api_endpoint}/health", timeout=10)
        assert resp.status_code == 200, f"Unexpected: {resp.status_code} {resp.text}"

    def test_health_returns_ok(self, api_endpoint):
        resp = requests.get(f"{api_endpoint}/health", timeout=10)
        body = resp.json()
        assert body.get("status") in ("ok", "healthy", "OK"), \
            f"Unexpected health body: {body}"

    def test_status_authenticated(self, api, api_endpoint):
        """GET /status returns 200 with authenticated request."""
        resp = api.get(f"{api_endpoint}/status", timeout=10)
        assert resp.status_code == 200, f"Unexpected: {resp.status_code} {resp.text}"

    def test_status_lists_tools(self, api, api_endpoint):
        """Status response includes the five tool names."""
        resp = api.get(f"{api_endpoint}/status", timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        tools = body.get("tools", [])
        for tool in ("analyze", "generate", "research", "summarize", "code"):
            assert tool in tools, f"Tool '{tool}' missing from status: {body}"

    def test_status_lists_providers(self, api, api_endpoint):
        """Status response includes at least bedrock in available providers."""
        resp = api.get(f"{api_endpoint}/status", timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        # Response shape: {"providers": {"bedrock": {"available": true, ...}, ...}}
        providers = body.get("providers", {})
        assert "bedrock" in providers, \
            f"bedrock not in providers: {body}"
        assert providers["bedrock"].get("available") is True, \
            f"bedrock not available: {providers['bedrock']}"
