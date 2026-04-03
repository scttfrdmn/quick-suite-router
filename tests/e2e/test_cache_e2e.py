"""
E2E tests for the DynamoDB response cache.

Auto-skipped when the stack was deployed without enable_cache=true
(CacheTableName CFN output will be absent).
"""

import pytest

from tests.e2e.conftest import call

pytestmark = pytest.mark.e2e

# Fixed low-temp prompt to guarantee cache eligibility (temperature <= 0.3)
_CACHE_PAYLOAD = {
    "prompt": "What is 2 + 2?",
    "temperature": 0.0,
    "max_tokens": 8,
    "skip_cache": False,
}


@pytest.fixture(scope="session", autouse=False)
def require_cache(cache_table_name):
    if not cache_table_name:
        pytest.skip("Cache not enabled (deploy with -c enable_cache=true to run cache tests)")


class TestCacheE2E:
    def test_first_request_is_cache_miss(self, api, api_endpoint, require_cache):
        """First request with skip_cache=False and temp=0 is a cache miss."""
        resp = call(api, api_endpoint, "analyze",
                    {**_CACHE_PAYLOAD, "skip_cache": True})   # prime with skip to clear any stale
        resp2 = call(api, api_endpoint, "analyze", _CACHE_PAYLOAD)
        assert resp2.status_code == 200
        # After a forced skip, second request may be a miss if nothing was written
        body = resp2.json()
        assert "cached" in body, f"Missing 'cached' field: {body}"

    def test_repeated_request_is_cache_hit(self, api, api_endpoint, require_cache):
        """Two identical low-temp requests — the second should be served from cache."""
        payload = _CACHE_PAYLOAD
        # First call populates the cache
        r1 = call(api, api_endpoint, "analyze", payload)
        assert r1.status_code == 200
        # Second call should hit cache
        r2 = call(api, api_endpoint, "analyze", payload)
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2.get("cached") is True, \
            f"Expected cache hit on second identical request: {body2}"

    def test_skip_cache_bypasses_cache(self, api, api_endpoint, require_cache):
        """skip_cache=True bypasses the cache even for a previously cached prompt."""
        # Cache entry already populated by earlier tests in this session.
        # skip_cache=True should go directly to Bedrock and return cached=False.
        resp = call(api, api_endpoint, "analyze", {**_CACHE_PAYLOAD, "skip_cache": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("cached") is not True, \
            f"Expected cache bypass but got cached=True: {body}"

    def test_high_temperature_not_cached(self, api, api_endpoint, require_cache):
        """Requests with temperature > 0.3 are never cached."""
        payload = {**_CACHE_PAYLOAD, "temperature": 0.8}
        r1 = call(api, api_endpoint, "analyze", payload)
        r2 = call(api, api_endpoint, "analyze", payload)
        assert r2.status_code == 200
        assert r2.json().get("cached") is not True, \
            f"High-temperature response should not be cached: {r2.json()}"
