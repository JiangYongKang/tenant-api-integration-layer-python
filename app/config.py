"""租户、权限、限流与开关配置。

配置只允许来自本地文件/进程内管理接口，不依赖任何外部服务。
运行中切换采用原子快照替换：一次请求内看到的配置永远是同一版本，
不会出现半切换；非法配置在校验阶段被拒绝，旧配置继续生效。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.constants import KNOWN_SCOPES, SAFE_DEFAULTS

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_HASH_RE = re.compile(r"^[a-f0-9]{16,128}$")

MIN_WINDOW_MS = 1
MAX_WINDOW_MS = 24 * 60 * 60 * 1_000
MIN_LIMIT = 1
MIN_BODY_BYTES = 1
MAX_BODY_BYTES = 10 * 1024 * 1024  # 硬上限，配置不得超过此值


@dataclass(frozen=True)
class TenantCredential:
    token_hash: str          # 仅保存 SHA-256 指纹，不保存完整凭证
    label: str               # 便于排障的非敏感名称
    scopes: frozenset[str]
    expires_at: int | None   # Unix 秒；None 表示不过期


@dataclass(frozen=True)
class RateLimitRule:
    limit: int               # 窗口内最大请求数
    window_ms: int           # 窗口长度（毫秒）


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    credentials: tuple[TenantCredential, ...]
    scopes: frozenset[str]
    # endpoint_key -> 限流规则；"default" 为租户兜底
    rate_limits: dict[str, RateLimitRule] = field(default_factory=dict)
    # 开关名 -> 是否启用
    flags: dict[str, bool] = field(default_factory=dict)
    max_body_bytes: int = 65_536


@dataclass(frozen=True)
class Settings:
    config_version: int
    tenants: dict[str, TenantConfig]
    # 全局默认开关（安全默认值：新能力默认关闭）
    default_flags: dict[str, bool] = field(default_factory=dict)
    default_max_body_bytes: int = 65_536
    default_rate_limit: RateLimitRule = RateLimitRule(limit=100, window_ms=60_000)


class ConfigError(ValueError):
    """配置非法（对外映射为 config_invalid，仅管理接口可能返回）。"""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"配置非法 @ {path}: {reason}")
        self.path = path
        self.reason = reason


def _require_type(raw: Any, path: str, typ: type | tuple[type, ...]) -> Any:
    if not isinstance(raw, typ):
        want = typ.__name__ if isinstance(typ, type) else "/".join(t.__name__ for t in typ)
        raise ConfigError(path, f"期望类型 {want}，实际 {type(raw).__name__}")
    return raw


def _parse_rule(raw: Any, path: str) -> RateLimitRule:
    _require_type(raw, path, dict)
    keys = set(raw.keys()) - {"limit", "window_ms"}
    if keys:
        raise ConfigError(path, f"出现未知字段 {sorted(keys)}")
    limit = _require_type(raw.get("limit"), f"{path}.limit", int)
    window_ms = _require_type(raw.get("window_ms"), f"{path}.window_ms", int)
    if limit < MIN_LIMIT:
        raise ConfigError(f"{path}.limit", f"必须 >= {MIN_LIMIT}，实际 {limit}")
    if not (MIN_WINDOW_MS <= window_ms <= MAX_WINDOW_MS):
        raise ConfigError(f"{path}.window_ms", f"必须在 [{MIN_WINDOW_MS}, {MAX_WINDOW_MS}] 毫秒内")
    return RateLimitRule(limit=limit, window_ms=window_ms)


def _parse_credential(raw: Any, path: str, tenant_scopes: frozenset[str]) -> TenantCredential:
    _require_type(raw, path, dict)
    keys = set(raw.keys()) - {"token_hash", "label", "scopes", "expires_at"}
    if keys:
        raise ConfigError(path, f"出现未知字段 {sorted(keys)}")
    token_hash = _require_type(raw.get("token_hash"), f"{path}.token_hash", str)
    if not _HASH_RE.match(token_hash):
        raise ConfigError(f"{path}.token_hash", "必须是 16-128 位十六进制指纹")
    label = _require_type(raw.get("label"), f"{path}.label", str)
    if not (1 <= len(label) <= 64):
        raise ConfigError(f"{path}.label", "长度必须在 1-64 字符之间")
    scopes_raw = _require_type(raw.get("scopes"), f"{path}.scopes", list)
    scopes: set[str] = set()
    for i, s in enumerate(scopes_raw):
        if not isinstance(s, str) or s not in KNOWN_SCOPES:
            raise ConfigError(f"{path}.scopes[{i}]", f"未知或非法权限: {s!r}")
        scopes.add(s)
    if not scopes:
        raise ConfigError(f"{path}.scopes", "凭证至少需要一个权限范围")
    if not scopes <= tenant_scopes:
        raise ConfigError(f"{path}.scopes", f"凭证权限 {sorted(scopes - tenant_scopes)} 超出租户授权")
    expires = raw.get("expires_at")
    if expires is not None:
        _require_type(expires, f"{path}.expires_at", int)
        if expires < 0:
            raise ConfigError(f"{path}.expires_at", "不能为负数")
    return TenantCredential(token_hash, label, frozenset(scopes), expires)


def _parse_tenant(tenant_id: str, raw: Any) -> TenantConfig:
    path = f"tenants.{tenant_id}"
    _require_type(raw, path, dict)
    if not _TENANT_RE.match(tenant_id):
        raise ConfigError(path, "租户标识需匹配 ^[a-z0-9][a-z0-9_-]{0,63}$")
    keys = set(raw.keys()) - {
        "credentials", "scopes", "rate_limits", "flags", "max_body_bytes",
    }
    if keys:
        raise ConfigError(path, f"出现未知字段 {sorted(keys)}")

    scopes_raw = _require_type(raw.get("scopes"), f"{path}.scopes", list)
    scopes = set()
    for s in scopes_raw:
        if not isinstance(s, str) or s not in KNOWN_SCOPES:
            raise ConfigError(f"{path}.scopes", f"未知或非法权限: {s!r}")
        scopes.add(s)

    creds_raw = _require_type(raw.get("credentials"), f"{path}.credentials", list)
    credentials = tuple(
        _parse_credential(c, f"{path}.credentials[{i}]", frozenset(scopes))
        for i, c in enumerate(creds_raw)
    )
    seen_labels = [c.label for c in credentials]
    if len(seen_labels) != len(set(seen_labels)):
        raise ConfigError(f"{path}.credentials", "同一租户下凭证 label 不得重复")

    rate_raw = raw.get("rate_limits", {})
    _require_type(rate_raw, f"{path}.rate_limits", dict)
    rules = {k: _parse_rule(v, f"{path}.rate_limits.{k}") for k, v in rate_raw.items()}

    flags_raw = raw.get("flags", {})
    _require_type(flags_raw, f"{path}.flags", dict)
    for name, value in flags_raw.items():
        if name not in SAFE_DEFAULTS:
            raise ConfigError(f"{path}.flags.{name}", "未知开关名")
        if not isinstance(value, bool):
            raise ConfigError(f"{path}.flags.{name}", "开关值必须是布尔值")

    max_body = raw.get("max_body_bytes", 65_536)
    _require_type(max_body, f"{path}.max_body_bytes", int)
    if not (MIN_BODY_BYTES <= max_body <= MAX_BODY_BYTES):
        raise ConfigError(
            f"{path}.max_body_bytes",
            f"必须在 [{MIN_BODY_BYTES}, {MAX_BODY_BYTES}] 字节内",
        )

    return TenantConfig(
        tenant_id=tenant_id,
        credentials=credentials,
        scopes=frozenset(scopes),
        rate_limits=dict(sorted(rules.items())),
        flags=dict(sorted(flags_raw.items())),
        max_body_bytes=max_body,
    )


def validate(raw: Any) -> Settings:
    """校验并构造不可变配置快照；任何非法项抛 ConfigError。"""
    _require_type(raw, "$", dict)
    top_keys = set(raw.keys()) - {
        "config_version", "tenants", "default_flags",
        "default_max_body_bytes", "default_rate_limit",
    }
    if top_keys:
        raise ConfigError("$", f"出现未知顶层字段 {sorted(top_keys)}")

    version = _require_type(raw.get("config_version"), "config_version", int)
    if version < 1:
        raise ConfigError("config_version", "必须为 >= 1 的整数")

    tenants_raw = _require_type(raw.get("tenants"), "tenants", dict)
    if not tenants_raw:
        raise ConfigError("tenants", "至少需要配置一个租户")
    tenants = {tid: _parse_tenant(tid, body) for tid, body in tenants_raw.items()}

    flags_raw = raw.get("default_flags", {})
    _require_type(flags_raw, "default_flags", dict)
    default_flags: dict[str, bool] = {}
    for name, value in flags_raw.items():
        if name not in SAFE_DEFAULTS:
            raise ConfigError(f"default_flags.{name}", "未知开关名")
        if not isinstance(value, bool):
            raise ConfigError(f"default_flags.{name}", "开关值必须是布尔值")
        default_flags[name] = value

    max_body = raw.get("default_max_body_bytes", 65_536)
    _require_type(max_body, "default_max_body_bytes", int)
    if not (MIN_BODY_BYTES <= max_body <= MAX_BODY_BYTES):
        raise ConfigError("default_max_body_bytes", f"必须在 [{MIN_BODY_BYTES}, {MAX_BODY_BYTES}] 字节内")

    default_rule = _parse_rule(
        raw.get("default_rate_limit", {"limit": 100, "window_ms": 60_000}),
        "default_rate_limit",
    )
    return Settings(
        config_version=version,
        tenants=dict(sorted(tenants.items())),
        default_flags=dict(sorted(default_flags.items())),
        default_max_body_bytes=max_body,
        default_rate_limit=default_rule,
    )


def load_settings(path: str | Path) -> Settings:
    """从本地 JSON 文件加载并校验配置。"""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(str(p), "配置文件不存在") from None
    except json.JSONDecodeError as e:
        raise ConfigError(str(p), f"JSON 解析失败: 行 {e.lineno} 列 {e.colno}") from None
    return validate(raw)


def _mask_hash(token_hash: str) -> str:
    """对已存储的指纹再做展示级脱敏。"""
    return f"{token_hash[:6]}…(redacted)"


def dump_safe(settings: Settings) -> dict[str, Any]:
    """导出配置视图：token_hash 等敏感字段必须脱敏/剔除。"""
    return {
        "config_version": settings.config_version,
        "default_max_body_bytes": settings.default_max_body_bytes,
        "default_rate_limit": {
            "limit": settings.default_rate_limit.limit,
            "window_ms": settings.default_rate_limit.window_ms,
        },
        "default_flags": dict(SAFE_DEFAULTS | settings.default_flags),
        "tenants": {
            tid: {
                "tenant_id": t.tenant_id,
                "scopes": sorted(t.scopes),
                "max_body_bytes": t.max_body_bytes,
                "rate_limits": {
                    k: {"limit": r.limit, "window_ms": r.window_ms}
                    for k, r in t.rate_limits.items()
                },
                "flags": dict(SAFE_DEFAULTS | t.flags),
                "credentials": [
                    {
                        "label": c.label,
                        "token_hash": _mask_hash(c.token_hash),
                        "scopes": sorted(c.scopes),
                        "expires_at": c.expires_at,
                    }
                    for c in t.credentials
                ],
            }
            for tid, t in settings.tenants.items()
        },
    }


def to_json(settings: Settings) -> str:
    """供管理接口回显的安全 JSON。"""
    return json.dumps(dump_safe(settings), ensure_ascii=False, sort_keys=True)
