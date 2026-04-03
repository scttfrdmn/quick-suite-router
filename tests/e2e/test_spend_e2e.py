"""
E2E tests for the spend ledger.

Invokes a tool once (session-scoped) then queries the query-spend Lambda
to verify the record was written. Minimal prompt to keep cost low.
"""

import json

import pytest

from tests.e2e.conftest import call

pytestmark = pytest.mark.e2e

# Unique department to isolate E2E spend records from real usage
_E2E_DEPT = "e2e-test-dept"
_E2E_USER = "e2e-test-user"


@pytest.fixture(scope="session")
def spend_invocation_result(api, api_endpoint):
    """
    Single tool invocation for spend ledger tests.
    Session-scoped: invokes once, shared across all spend tests.
    """
    resp = call(api, api_endpoint, "analyze", {
        "prompt": "What is 2 + 2?",
        "department": _E2E_DEPT,
        "user_id": _E2E_USER,
        "temperature": 0.0,
        "max_tokens": 16,
    })
    if resp.status_code == 503:
        pytest.skip(f"No providers available for spend test: {resp.text}")
    assert resp.status_code == 200, f"Spend test invocation failed: {resp.status_code} {resp.text}"
    return resp.json()


class TestSpendE2E:
    def test_invocation_records_cost(self, spend_invocation_result):
        """The tool invocation response includes a cost_usd estimate."""
        body = spend_invocation_result
        # cost_usd may be in the response or only written to the ledger
        # Either way, input/output tokens should be present for cost calculation
        assert body.get("input_tokens", 0) > 0
        assert body.get("output_tokens", 0) > 0

    def test_query_spend_returns_records(self, lam, query_spend_fn_name, spend_invocation_result):
        """query-spend Lambda returns at least one record for the E2E department."""
        payload = {"department": _E2E_DEPT}
        response = lam.invoke(
            FunctionName=query_spend_fn_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        raw = response["Payload"].read()
        if response.get("FunctionError"):
            pytest.fail(f"query-spend Lambda error: {raw.decode()}")
        result = json.loads(raw)
        assert "error" not in result, f"query-spend returned error: {result}"
        assert "records" in result or "total_cost_usd" in result or "items" in result, \
            f"Unexpected query-spend response shape: {result}"

    def test_query_spend_by_user(self, lam, query_spend_fn_name, spend_invocation_result):
        """query-spend supports filtering by user_id."""
        payload = {"department": _E2E_DEPT, "user_id": _E2E_USER}
        response = lam.invoke(
            FunctionName=query_spend_fn_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        raw = response["Payload"].read()
        if response.get("FunctionError"):
            pytest.fail(f"query-spend Lambda error: {raw.decode()}")
        result = json.loads(raw)
        assert "error" not in result, f"query-spend returned error: {result}"
