"""按租户与接口维度的限流。

- 固定窗口计数，键为 (tenant_id, endpoint_key)，天然无跨租户串扰；
- 超限返回 429 + Retry-After-Ms 与机器可读 rate_limited 错误；
- 突发流量只消耗本租户本接口的窗口，不影响其他租户。
"""

from __future__ import annotations

from app.config import RateLimitRule, TenantConfig
from app.errors import APIError, ErrorCode
from app.stores import RateLimitStore

# endpoint_key 采用 "METHOD PATH_TEMPLATE"，例如 "POST /api/v1/widgets"
DEFAULT_ENDPOINT_KEY = "default"


def resolve_rule(tenant: TenantConfig, endpoint_key: str, fallback: RateLimitRule) -> RateLimitRule:
    """精确接口规则 -> 租户兜底("default") -> 全局兜底。"""
    if endpoint_key in tenant.rate_limits:
        return tenant.rate_limits[endpoint_key]
    if DEFAULT_ENDPOINT_KEY in tenant.rate_limits:
        return tenant.rate_limits[DEFAULT_ENDPOINT_KEY]
    return fallback


def enforce(
    store: RateLimitStore,
    tenant_id: str,
    endpoint_key: str,
    rule: RateLimitRule,
    now_ms: int,
) -> int | None:
    """放行返回 None；超限返回需等待的毫秒数（Retry-After-Ms）。"""
    allowed, count, reset_in_ms = store.hit(
        tenant_id, endpoint_key, now_ms, rule.limit, rule.window_ms
    )
    if allowed:
        return None
    raise APIError(
        ErrorCode.RATE_LIMITED,
        429,
        f"超出租户在该接口的请求限额（每 {rule.window_ms}ms 最多 {rule.limit} 次），"
        "请按 Retry-After-Ms 提示退避后重试",
        details={
            "limit": rule.limit,
            "window_ms": rule.window_ms,
            "endpoint": endpoint_key,
            "retry_after_ms": reset_in_ms,
        },
        retry_after_ms=reset_in_ms,
    )
