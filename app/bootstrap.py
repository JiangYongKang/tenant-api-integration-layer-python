"""本地引导配置：内置一份演示用租户/凭证/限流/开关配置。

仅用于本地与测试，所有数据均为本地生成，无真实凭据、无外部依赖。
明文令牌仅通过返回值交给测试/本地启动日志，**不会写入配置文件或响应**。
"""

from __future__ import annotations

from app.config import (
    RateLimitRule,
    Settings,
    TenantConfig,
    TenantCredential,
    validate,
)
from app.credentials import MintResult, mint_token

# 端点键：方法 + 空格 + 具体版本路径模板
EP_V1_WIDGETS_WRITE = "POST /api/v1/widgets"
EP_V2_WIDGETS_WRITE = "POST /api/v2/widgets"


def build_default_settings(now: int) -> tuple[Settings, dict[str, dict[str, str]]]:
    """构造内置配置，返回 (配置, {租户: {label: 明文令牌}})。

    内置三个租户，覆盖典型场景：
    - tenant-a：读写权限、宽松限流、insights 开启；
    - tenant-b：只读 + 一个已过期凭证、严格写限流（用于限流测试）；
    - tenant-c：只写、小请求体上限（用于超限测试）。
    """
    tokens: dict[str, dict[str, str]] = {}

    def cred(tenant: str, label: str, scopes: list[str], ttl: int | None) -> TenantCredential:
        result: MintResult = mint_token(tenant, label, ttl, now)
        tokens.setdefault(tenant, {})[label] = result.token
        return TenantCredential(
            token_hash=result.token_hash,
            label=label,
            scopes=frozenset(scopes),
            expires_at=None if ttl is None else now + ttl,
        )

    tenant_a = TenantConfig(
        tenant_id="tenant-a",
        credentials=(
            cred("tenant-a", "ci-writer", ["widgets:write", "widgets:read"], ttl=3600),
            cred("tenant-a", "ci-reader", ["widgets:read"], ttl=3600),
        ),
        scopes=frozenset({"widgets:write", "widgets:read"}),
        rate_limits={
            "default": RateLimitRule(limit=1000, window_ms=60_000),
        },
        flags={"widgets.insights": True, "widgets.include_metadata": False},
        max_body_bytes=65_536,
    )
    tenant_b = TenantConfig(
        tenant_id="tenant-b",
        credentials=(
            cred("tenant-b", "writer", ["widgets:write", "widgets:read"], ttl=3600),
            cred("tenant-b", "expired-one", ["widgets:read"], ttl=-100),
        ),
        scopes=frozenset({"widgets:write", "widgets:read"}),
        rate_limits={
            EP_V1_WIDGETS_WRITE: RateLimitRule(limit=3, window_ms=1_000),
            EP_V2_WIDGETS_WRITE: RateLimitRule(limit=3, window_ms=1_000),
            "default": RateLimitRule(limit=100, window_ms=60_000),
        },
        flags={"widgets.insights": False},
        max_body_bytes=65_536,
    )
    tenant_c = TenantConfig(
        tenant_id="tenant-c",
        credentials=(
            cred("tenant-c", "tiny-writer", ["widgets:write", "widgets:read"], ttl=3600),
        ),
        scopes=frozenset({"widgets:write", "widgets:read"}),
        rate_limits={"default": RateLimitRule(limit=1000, window_ms=60_000)},
        flags={},
        max_body_bytes=256,
    )
    tenant_admin = TenantConfig(
        tenant_id="local-admin",
        credentials=(
            cred("local-admin", "ops", ["config:read", "config:write"], ttl=3600),
        ),
        scopes=frozenset({"config:read", "config:write"}),
        rate_limits={"default": RateLimitRule(limit=100, window_ms=60_000)},
        flags={},
        max_body_bytes=65_536,
    )

    def tenant_raw(t: TenantConfig) -> dict:
        return {
            "scopes": sorted(t.scopes),
            "credentials": [
                {
                    "token_hash": c.token_hash,
                    "label": c.label,
                    "scopes": sorted(c.scopes),
                    "expires_at": c.expires_at,
                }
                for c in t.credentials
            ],
            "rate_limits": {
                k: {"limit": r.limit, "window_ms": r.window_ms}
                for k, r in t.rate_limits.items()
            },
            "flags": t.flags,
            "max_body_bytes": t.max_body_bytes,
        }

    settings = validate(
        {
            "config_version": 1,
            "default_flags": {"widgets.insights": False, "widgets.include_metadata": False},
            "default_max_body_bytes": 65_536,
            "default_rate_limit": {"limit": 200, "window_ms": 60_000},
            "tenants": {
                t.tenant_id: tenant_raw(t)
                for t in (tenant_a, tenant_b, tenant_c, tenant_admin)
            },
        }
    )
    return settings, tokens
