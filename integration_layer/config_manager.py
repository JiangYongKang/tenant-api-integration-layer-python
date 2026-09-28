"""运行时配置管理：不可变快照 + 单调配置版本 + 有界历史 + 原子提交/回退。

设计要点：
- RuntimeConfig 是不可变快照（凭证、限流规则、灰度开关）。一次请求持有同一个
  快照引用，配置切换不会把在途请求带进半切换/中间状态。
- 每次生效的变更得到一个单调递增的版本号；指针替换是单次原子赋值，不存在半切换。
- 所有校验在指针替换之前完成：非法输入抛 INVALID_CONFIG，正在生效的版本原样保留。
- 历史只保留最近 history_size 个版本，超出丢弃最旧；回退产生新版本号（内容等于
  旧版本）。回退目标已不存在或内容不可用时抛 CONFIG_VERSION_NOT_FOUND，
  与“配置本身非法”（INVALID_CONFIG）明确区分。
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .auth import TenantCredential
from .errors import ApiError, ErrorCode
from .feature_flags import FlagRule, FlagSnapshot
from .rate_limit import RateLimitRule

DEFAULT_HISTORY_SIZE = 20

KNOWN_SCOPES = frozenset({"resources:read", "resources:write"})


def secret_fingerprint(secret: str) -> str:
    """凭证指纹：sha256 前 12 位，不可逆，用于响应/日志核对。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def mask_secret(secret: str) -> str:
    """脱敏展示：绝不回显完整密钥，只保留可核对的前缀/后缀片段。"""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}-****-{secret[-2:]}"


@dataclass(frozen=True)
class RuntimeConfig:
    """某一时刻生效的完整配置；全部成员不可变。"""

    version: int
    credentials: Mapping[str, TenantCredential]
    rate_rules: Mapping[tuple[str, str], RateLimitRule]
    flags: Mapping[str, FlagRule]

    def flag_snapshot(self) -> FlagSnapshot:
        return FlagSnapshot(flags=dict(self.flags))

    def public_view(self) -> dict:
        """对外/日志可见的配置视图：凭证只含指纹与脱敏信息，绝无密钥。"""
        return {
            "version": self.version,
            "credentials": [
                {
                    "key_id": c.key_id,
                    "tenant_id": c.tenant_id,
                    "fingerprint": c.fingerprint,
                    "scopes": sorted(c.scopes),
                    "expires_at": c.expires_at,
                }
                for c in self.credentials.values()
            ],
            "rate_rules": [
                {
                    "tenant_id": t,
                    "endpoint": ep,
                    "capacity": r.capacity,
                    "refill_per_second": r.refill_per_second,
                }
                for (t, ep), r in self.rate_rules.items()
            ],
            "flags": {
                name: {"rollout": rule.rollout, "overrides": dict(rule.overrides)}
                for name, rule in self.flags.items()
            },
        }


class ConfigManager:
    """线程安全的配置版本管理器。"""

    def __init__(
        self,
        initial_rate_rules: dict[tuple[str, str], RateLimitRule] | None = None,
        initial_flags: dict[str, FlagRule] | None = None,
        history_size: int = DEFAULT_HISTORY_SIZE,
    ) -> None:
        if history_size < 1:
            raise ApiError(ErrorCode.INVALID_CONFIG, "history_size must be >= 1")
        self._lock = threading.Lock()
        self._history_size = history_size
        first = self._build(
            version=1,
            credentials={},
            rate_rules=dict(initial_rate_rules or {}),
            flags=dict(initial_flags or {}),
        )
        # OrderedDict: version -> RuntimeConfig，始终包含 current，有界
        self._history: OrderedDict[int, RuntimeConfig] = OrderedDict()
        self._history[1] = first
        self._current = first
        self._next_version = 2

    # ---- 读取 ----

    def current(self) -> RuntimeConfig:
        with self._lock:
            return self._current

    def history_versions(self) -> list[int]:
        with self._lock:
            return list(self._history.keys())

    # ---- 凭证：签发 / 轮换 / 吊销 ----

    def issue_credential(
        self,
        tenant_id: str,
        scopes: set[str] | frozenset[str],
        expires_at: float | None = None,
        secret: str | None = None,
    ) -> tuple[str, str]:
        """签发新凭证；完整 secret 仅此处返回一次。返回 (key_id, secret)。"""
        self._validate_tenant(tenant_id)
        scope_set = self._validate_scopes(scopes)
        self._validate_expires(expires_at)
        secret = secret or secrets.token_urlsafe(32)
        if not isinstance(secret, str) or not secret:
            raise ApiError(ErrorCode.INVALID_CONFIG, "secret must be a non-empty string")

        def build(cfg: RuntimeConfig, ver: int):
            key_id = f"{tenant_id}.{secrets.token_hex(4)}"
            # 极低概率碰撞时直接拒绝，保持键唯一
            if key_id in cfg.credentials:
                raise ApiError(ErrorCode.INVALID_CONFIG, "key_id collision, retry")
            cred = TenantCredential(
                tenant_id=tenant_id,
                key_id=key_id,
                secret_hash=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                scopes=scope_set,
                expires_at=expires_at,
            )
            creds = dict(cfg.credentials)
            creds[key_id] = cred
            return self._build(ver, creds, dict(cfg.rate_rules), dict(cfg.flags)), key_id

        with self._lock:
            cfg, key_id = self._commit(build)
        return key_id, secret

    def rotate_credential(self, key_id: str, secret: str | None = None) -> str:
        """轮换密钥：key_id/租户/scope 不变，仅替换密钥哈希。返回新明文（仅一次）。"""
        secret = secret or secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()

        def build(cfg: RuntimeConfig, ver: int):
            old = cfg.credentials.get(key_id)
            if old is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"credential not found: {key_id}")
            creds = dict(cfg.credentials)
            creds[key_id] = TenantCredential(
                tenant_id=old.tenant_id,
                key_id=old.key_id,
                secret_hash=new_hash,
                scopes=old.scopes,
                expires_at=old.expires_at,
            )
            return self._build(ver, creds, dict(cfg.rate_rules), dict(cfg.flags)), None

        with self._lock:
            self._commit(build)
        return secret

    def revoke_credential(self, key_id: str) -> bool:
        """吊销凭证；幂等——重复吊销返回 False，不产生额外配置版本。"""

        def build(cfg: RuntimeConfig, ver: int):
            if key_id not in cfg.credentials:
                raise _NoChange()
            creds = dict(cfg.credentials)
            creds.pop(key_id)
            return self._build(ver, creds, dict(cfg.rate_rules), dict(cfg.flags)), None

        with self._lock:
            try:
                self._commit(build)
            except _NoChange:
                return False
        return True

    # ---- 限流 / 灰度调整 ----

    def set_rate_rule(self, tenant_id: str, endpoint: str, rule: RateLimitRule) -> None:
        self._validate_tenant(tenant_id)
        if not isinstance(endpoint, str) or not endpoint:
            raise ApiError(ErrorCode.INVALID_CONFIG, "endpoint must be a non-empty string")
        # RateLimitRule 构造时已校验正数
        def build(cfg: RuntimeConfig, ver: int):
            rules = dict(cfg.rate_rules)
            rules[(tenant_id, endpoint)] = rule
            return self._build(ver, dict(cfg.credentials), rules, dict(cfg.flags)), None

        with self._lock:
            self._commit(build)

    def replace_flags(self, flags: dict[str, FlagRule]) -> None:
        """整体替换灰度开关（保留旧 FlagStore.replace 语义）。"""
        def build(cfg: RuntimeConfig, ver: int):
            return self._build(ver, dict(cfg.credentials), dict(cfg.rate_rules), dict(flags)), None

        with self._lock:
            self._commit(build)

    def set_flag(self, name: str, rule: FlagRule) -> None:
        if not isinstance(name, str) or not name:
            raise ApiError(ErrorCode.INVALID_CONFIG, "flag name must be a non-empty string")
        # FlagRule 构造时已校验取值范围
        def build(cfg: RuntimeConfig, ver: int):
            flags = dict(cfg.flags)
            flags[name] = rule
            return self._build(ver, dict(cfg.credentials), dict(cfg.rate_rules), flags), None

        with self._lock:
            self._commit(build)

    # ---- 回退 ----

    def rollback(self, target_version: int) -> RuntimeConfig:
        """原子回退到历史版本的内容（以新版本号生效）。

        目标版本已被有界历史丢弃、或快照不可用时 -> CONFIG_VERSION_NOT_FOUND，
        与提交非法配置的 INVALID_CONFIG 明确区分；当前生效版本不受影响。
        """
        if not isinstance(target_version, int) or isinstance(target_version, bool):
            raise ApiError(
                ErrorCode.CONFIG_VERSION_NOT_FOUND,
                f"unknown config version: {target_version!r}",
            )
        with self._lock:
            target = self._history.get(target_version)
            if target is None:
                raise ApiError(
                    ErrorCode.CONFIG_VERSION_NOT_FOUND,
                    f"config version not found: {target_version}",
                    details={"available_versions": list(self._history.keys())},
                )
            try:
                return self._commit(
                    lambda cfg, ver: (
                        self._build(
                            ver,
                            dict(target.credentials),
                            dict(target.rate_rules),
                            dict(target.flags),
                        ),
                        None,
                    )
                )[0]
            except ApiError:
                # 历史快照理论上始终合法；一旦不可用，按“版本不可用”区分对待
                raise ApiError(
                    ErrorCode.CONFIG_VERSION_NOT_FOUND,
                    f"config version not usable: {target_version}",
                )

    # ---- 内部 ----

    def _commit(self, builder) -> tuple[RuntimeConfig, object]:
        """在锁内：基于当前快照构造下一版本，校验通过才原子替换并记入有界历史。"""
        prev = self._current
        new_cfg, extra = builder(prev, self._next_version)
        # 指针单次赋值：任何后续请求看到的都是完整的新配置
        self._current = new_cfg
        self._history[new_cfg.version] = new_cfg
        while len(self._history) > self._history_size:
            self._history.popitem(last=False)
        self._next_version += 1
        return new_cfg, extra

    @staticmethod
    def _build(version, credentials, rate_rules, flags) -> RuntimeConfig:
        return RuntimeConfig(
            version=version,
            credentials=MappingProxyType(dict(credentials)),
            rate_rules=MappingProxyType(dict(rate_rules)),
            flags=MappingProxyType(dict(flags)),
        )

    @staticmethod
    def _validate_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ApiError(ErrorCode.INVALID_CONFIG, "tenant_id must be a non-empty string")

    @staticmethod
    def _validate_scopes(scopes) -> frozenset[str]:
        if not scopes or not isinstance(scopes, (set, frozenset)):
            raise ApiError(ErrorCode.INVALID_CONFIG, "scopes must be a non-empty set")
        for s in scopes:
            if not isinstance(s, str) or not s:
                raise ApiError(ErrorCode.INVALID_CONFIG, "scope must be a non-empty string")
            if s not in KNOWN_SCOPES:
                raise ApiError(
                    ErrorCode.INVALID_CONFIG,
                    f"unknown scope: {s}",
                    details={"known": sorted(KNOWN_SCOPES)},
                )
        return frozenset(scopes)

    @staticmethod
    def _validate_expires(expires_at) -> None:
        if expires_at is not None and not isinstance(expires_at, (int, float)):
            raise ApiError(ErrorCode.INVALID_CONFIG, "expires_at must be epoch seconds or null")


class _NoChange(Exception):
    """内部信号：变更无效（如重复吊销），不产生新版本。"""
