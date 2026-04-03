"""
E2E tests for PHI routing.

Requests with data_classification=phi must be routed to Bedrock only,
regardless of the configured provider preference order.
"""

import pytest

from tests.e2e.conftest import call

pytestmark = pytest.mark.e2e

_FAST = {"temperature": 0.0, "max_tokens": 16}


class TestPhiRoutingE2E:
    def test_phi_request_routes_to_bedrock(self, api, api_endpoint):
        """data_classification=phi restricts routing to Bedrock."""
        resp = call(api, api_endpoint, "analyze", {
            "prompt": "What is 2 + 2?",
            "data_classification": "phi",
            **_FAST,
        })
        assert resp.status_code == 200, \
            f"PHI request failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body.get("provider") == "bedrock", \
            f"PHI request was not routed to bedrock: {body}"

    def test_phi_request_returns_content(self, api, api_endpoint):
        """PHI-tagged requests return valid content."""
        resp = call(api, api_endpoint, "summarize", {
            "prompt": "What is 2 + 2?",
            "data_classification": "phi",
            **_FAST,
        })
        assert resp.status_code == 200
        assert resp.json().get("content"), "Empty content on PHI request"

    def test_explicit_non_bedrock_ignored_in_phi_mode(self, api, api_endpoint):
        """
        An explicit provider override to a non-Bedrock provider is silently
        ignored when data_classification=phi. Response should still use bedrock.
        """
        resp = call(api, api_endpoint, "analyze", {
            "prompt": "What is 2 + 2?",
            "data_classification": "phi",
            "provider": "openai",   # should be ignored
            **_FAST,
        })
        # Either 200 (bedrock used) or 503 (openai not available and PHI blocks others)
        assert resp.status_code in (200, 503), \
            f"Unexpected status for PHI+openai override: {resp.status_code} {resp.text}"
        if resp.status_code == 200:
            assert resp.json().get("provider") == "bedrock", \
                f"Non-bedrock provider used for PHI request: {resp.json()}"
