"""限流：按租户+接口隔离，超限稳定拒绝并带重试提示，无跨租户串扰。"""
import pytest

from integration_layer.app import LayerConfig, create_app
from integration_layer.errors import ErrorCode
from integration_layer.rate_limit import RateLimiter, RateLimitRule
from fastapi.testclient import TestClient

from .conftest import log_case


@pytest.fixture()
def limited_app():
    return create_app(LayerConfig(default_rate_rule=RateLimitRule(capacity=3, refill_per_second=1.0)))


@pytest.fixture()
def limited_client(limited_app):
    return TestClient(limited_app, raise_server_exceptions=False)


def _register(app, tenant_id):
    key_id, secret = app.state.credentials.register(tenant_id, {"resources:read"})
    return {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1"}


def test_rate_limit_rejects_with_retry_hint(limited_app, limited_client):
    headers = _register(limited_app, "tenant-rl")
    codes = [limited_client.get("/api/resources/x", headers=headers).status_code for _ in range(3)]
    r = limited_client.get("/api/resources/x", headers=headers)
    log_case(
        "rate limit exceeded",
        {"capacity": 3, "requests": 4},
        "第 4 个请求应被 RATE_LIMITED 拒绝，响应含 retry_after 提示",
    )
    assert codes == [404, 404, 404]  # 前三个正常通过限流（资源不存在但非限流）
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == ErrorCode.RATE_LIMITED.value
    assert body["error"]["retry_after_seconds"] > 0
    assert "retry-after" in r.headers


def test_tenant_isolation_no_cross_talk(limited_app, limited_client):
    headers_a = _register(limited_app, "tenant-burst")
    headers_b = _register(limited_app, "tenant-quiet")
    for _ in range(4):
        limited_client.get("/api/resources/x", headers=headers_a)
    r_a = limited_client.get("/api/resources/x", headers=headers_a)
    r_b = limited_client.get("/api/resources/x", headers=headers_b)
    log_case(
        "tenant isolation",
        {"tenant-burst": "5 requests", "tenant-quiet": "1 request"},
        "突发租户被限流，其他租户不受影响（计数独立）",
    )
    assert r_a.status_code == 429
    assert r_b.status_code == 404


def test_endpoint_dimension_independent(limited_app, limited_client):
    headers = _register(limited_app, "tenant-dim")
    for _ in range(3):
        limited_client.get("/api/resources/x", headers=headers)
    # GET 已打满，POST 是另一维度，应仍被限流规则单独计数
    r = limited_client.post(
        "/api/resources",
        json={"name": "x"},
        headers={**headers, "idempotency-key": "k"},
    )
    log_case(
        "endpoint dimension",
        {"GET": "3 (exhausted)", "POST": "1"},
        "不同接口维度计数独立；POST 因缺写权限落在 PERMISSION_DENIED 而非 RATE_LIMITED",
    )
    assert r.status_code == 403


def test_invalid_rule_rejected():
    log_case("invalid rate rule", {"capacity": 0}, "非正容量/速率应被 INVALID_CONFIG 拒绝")
    with pytest.raises(Exception) as ei:
        RateLimitRule(capacity=0, refill_per_second=1.0)
    assert "INVALID_CONFIG" in str(ei.value)


def test_rule_update_resets_bucket():
    limiter = RateLimiter(RateLimitRule(capacity=1, refill_per_second=0.01))
    limiter.check("t", "ep", now=0.0)
    with pytest.raises(Exception):
        limiter.check("t", "ep", now=0.1)  # 已超限
    limiter.set_rule("t", "ep", RateLimitRule(capacity=5, refill_per_second=1.0))
    limiter.check("t", "ep", now=0.2)  # 配置更新后桶重置，立即可用
    log_case("rule update", {"capacity": "1 -> 5"}, "配置变更重置桶，避免半切换状态")
