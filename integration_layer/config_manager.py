"""运维配置管理：凭证/限流/灰度的原子整体生效、单调版本、有界历史与回退。

设计：
- 每次生效的配置是一个不可变 ConfigSnapshot（凭证表 + 限流规则 + 灰度开关），
  变更基于当前快照整体构造新快照并在单锁内切换当前指针——不存在改一半的状态；
  请求开始时拿到的快照引用在整个请求内不会被后续变更影响。
- 每次提交（含回退）产生一个严格单调递增的配置版本号。
- 历史有界（默认 10 个版本），超出丢弃最旧版本；当前生效版本绝不会被丢弃。
- 所有校验发生在切换之前：非法配置抛 INVALID_CONFIG，正在生效的旧快照原样保留。
- 回退目标不存在（从未有过或已被有界历史淘汰）-> CONFIG_VERSION_NOT_FOUND，
  与“配置内容本身非法”（INVALID_CONFIG）明确区分。
"""
from __future__ import annotations

import copy
import hashlib
import secrets
import threading
from dataclasses import dataclass, field

from .auth import TenantCredential
from .errors import ApiError, ErrorCode
from .feature_flags import FlagRule
from .rate_limit import RateLimitRule

_VALID_SCOPES = frozenset({"resources:read", "resources:write"})


@dataclass(frozen=True)
class ConfigSnapshot:
    """一次生效的整体配置：值均为不可变对象，浅拷贝字典即可隔离。"""

    credentials: dict[str, TenantCredential] = field(default_factory=dict)
    rate_rules: dict[tuple[str, str], RateLimitRule] = field(default_factory=dict)
    flags: dict[str, FlagRule] = field(default_factory=dict)


def mask_secret(secret: str) -> str:
    """对外/日志可核对的脱敏串：保留首 2 与末 2 位，中间固定掩码，不可逆推。"""
    if len(secret) <= 4:
        return "****"
    return f"{secret[:2]}****{secret[-2:]}"


def secret_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


class ConfigManager:
    def __init__(
        self,
        initial: ConfigSnapshot | None = None,
        history_limit: int = 10,
        default_rate_rule: RateLimitRule | None = None,
    ) -> None:
        if history_limit < 1:
            raise ApiError(ErrorCode.INVALID_CONFIG, "history_limit must be >= 1")
        self._lock = threading.Lock()
        self._history_limit = history_limit
        self.default_rate_rule = default_rate_rule
        first = self._freeze(initial or ConfigSnapshot())
        self._seq = 1
        self._current_version = 1
        # version -> 快照；_order 维护版本先后，便于有界淘汰最旧版本
        self._history: dict[int, ConfigSnapshot] = {1: first}
        self._order: list[int] = [1]

    # ---- 读取 ----

    def snapshot(self) -> ConfigSnapshot:
        with self._lock:
            return self._history[self._current_version]

    @property
    def current_version(self) -> int:
        with self._lock:
            return self._current_version

    def history_versions(self) -> list[int]:
        with self._lock:
            return list(self._order)

    # ---- 凭证 ----

    def issue_credential(
        self,
        tenant_id: str,
        scopes: set[str] | frozenset[str],
        secret: str | None = None,
        expires_at: float | None = None,
    ) -> tuple[str, str]:
        """签发新凭证，原子生效。返回 (key_id, secret)；secret 仅此一处明文出现。"""
        tenant_id = self._require_identifier(tenant_id, "tenant_id")
        scope_set = self._validate_scopes(scopes)
        secret = secret or secrets.token_urlsafe(32)
        self._validate_secret(secret)
        self._validate_expires(expires_at)
        key_id = f"{tenant_id}.{secrets.token_hex(4)}"
        cred = TenantCredential(
            tenant_id=tenant_id,
            key_id=key_id,
            secret_hash=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            scopes=scope_set,
            expires_at=expires_at,
        )

        def mutate(snap: ConfigSnapshot) -> None:
            snap.credentials[key_id] = cred

        self._commit(mutate)
        return key_id, secret

    def rotate_credential(
        self, key_id: str, secret: str | None = None, expires_at: float | None = None
    ) -> tuple[str, str]:
        """轮换凭证：换发新 key_id/secret 并原子吊销旧 key。旧 key 不存在 -> NOT_FOUND。"""
        if not key_id:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "key_id is required")
        secret = secret or secrets.token_urlsafe(32)
        self._validate_secret(secret)
        self._validate_expires(expires_at)

        def mutate(snap: ConfigSnapshot) -> tuple[str, str]:
            old = snap.credentials.get(key_id)
            if old is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"credential '{key_id}' not found")
            new_key_id = f"{old.tenant_id}.{secrets.token_hex(4)}"
            new_cred = TenantCredential(
                tenant_id=old.tenant_id,
                key_id=new_key_id,
                secret_hash=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                scopes=old.scopes,
                expires_at=expires_at,
            )
            del snap.credentials[key_id]
            snap.credentials[new_key_id] = new_cred
            return new_key_id, secret

        return self._commit(mutate)

    def revoke_credential(self, key_id: str) -> bool:
        """吊销凭证，原子生效；重复吊销幂等。返回该 key 吊销前是否存在。

        key 当前不存在（已吊销过 / 从未签发）时不发生任何实际变更：
        不提交新版本、不推进版本号，也不会在有界历史里占用槽位顶掉真正
        生效过的版本。接口仍稳定返回 False（existed=false），可解释。
        """
        if not key_id:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "key_id is required")
        with self._lock:
            current = self._history[self._current_version]
            if key_id not in current.credentials:
                # 无实际变更：幂等空操作，配置原样保留
                return False

            def mutate(snap: ConfigSnapshot) -> bool:
                return snap.credentials.pop(key_id, None) is not None

            return self._commit_locked(mutate)

    # ---- 限流 ----

    def set_rate_rule(
        self, tenant_id: str, endpoint: str, rule: RateLimitRule
    ) -> int:
        """按 (租户, 接口) 原子调整限流额度；rule 自身非法时由其构造函数拒绝。"""
        tenant_id = self._require_identifier(tenant_id, "tenant_id")
        endpoint = self._require_identifier(endpoint, "endpoint")
        if not isinstance(rule, RateLimitRule):
            raise ApiError(
                ErrorCode.INVALID_CONFIG, "rate rule must be a RateLimitRule instance"
            )

        def mutate(snap: ConfigSnapshot) -> None:
            snap.rate_rules[(tenant_id, endpoint)] = rule

        return self._commit(mutate)

    # ---- 灰度 ----

    def replace_flags(self, flags: dict[str, FlagRule]) -> int:
        """原子整体替换灰度开关；非法配置拒绝并保留正在生效的旧快照。"""
        if not isinstance(flags, dict):
            raise ApiError(ErrorCode.INVALID_CONFIG, "flags must be an object")
        normalized: dict[str, FlagRule] = {}
        for name, rule in flags.items():
            if not isinstance(name, str) or not name:
                raise ApiError(ErrorCode.INVALID_CONFIG, "flag name must be a non-empty string")
            if not isinstance(rule, FlagRule):
                raise ApiError(
                    ErrorCode.INVALID_CONFIG, f"flag '{name}' must be a FlagRule"
                )
            # 重建以隔离外部可变 dict，并触发完整校验
            normalized[name] = FlagRule(
                rollout=rule.rollout, overrides=dict(rule.overrides)
            )

        def mutate(snap: ConfigSnapshot) -> None:
            snap.flags.clear()
            snap.flags.update(normalized)

        return self._commit(mutate)

    def set_flag(self, name: str, rule: FlagRule) -> int:
        """调整单个开关；其余开关不动，仍整体原子生效。"""
        if not isinstance(name, str) or not name:
            raise ApiError(ErrorCode.INVALID_CONFIG, "flag name must be a non-empty string")
        if not isinstance(rule, FlagRule):
            raise ApiError(ErrorCode.INVALID_CONFIG, f"flag '{name}' must be a FlagRule")
        fresh = FlagRule(rollout=rule.rollout, overrides=dict(rule.overrides))
        snap = self.snapshot()
        merged = dict(snap.flags)
        merged[name] = fresh
        return self.replace_flags(merged)

    # ---- 回退 ----

    def rollback(self, version: int) -> ConfigSnapshot:
        """原子回退到历史版本：以其内容生成一个新的单调版本并整体切换。

        目标版本不存在（含已被有界历史淘汰）-> CONFIG_VERSION_NOT_FOUND。
        """
        if not isinstance(version, int) or isinstance(version, bool):
            raise ApiError(ErrorCode.VALIDATION_FAILED, "version must be an integer")
        with self._lock:
            target = self._history.get(version)
            if target is None:
                raise ApiError(
                    ErrorCode.CONFIG_VERSION_NOT_FOUND,
                    f"config version {version} does not exist",
                    details={
                        "requested_version": version,
                        "available_versions": list(self._order),
                    },
                )
            restored = self._freeze(target)
            return self._commit_locked(lambda snap: self._assign(snap, restored))

    # ---- 内部 ----

    def _commit(self, mutate) -> object:  # type: ignore[no-untyped-def]
        """基于当前快照构造候选快照；先执行 mutate（内含校验），通过后再切换。"""
        with self._lock:
            return self._commit_locked(mutate)

    def _commit_locked(self, mutate) -> object:  # type: ignore[no-untyped-def]
        base = self._history[self._current_version]
        candidate = self._freeze(base)
        result = mutate(candidate)  # 抛异常则候选被丢弃，正在生效的快照原样保留
        self._seq += 1
        new_version = self._seq
        self._history[new_version] = candidate
        self._order.append(new_version)
        self._current_version = new_version
        self._trim_locked()
        return result if result is not None else new_version

    def _trim_locked(self) -> None:
        while len(self._order) > self._history_limit:
            oldest = self._order[0]
            if oldest == self._current_version:
                break  # 当前生效版本绝不丢弃
            self._order.pop(0)
            self._history.pop(oldest, None)

    @staticmethod
    def _freeze(src: ConfigSnapshot) -> ConfigSnapshot:
        return ConfigSnapshot(
            credentials=dict(src.credentials),
            rate_rules=dict(src.rate_rules),
            flags={
                name: FlagRule(rollout=rule.rollout, overrides=dict(rule.overrides))
                for name, rule in src.flags.items()
            },
        )

    @staticmethod
    def _assign(dst: ConfigSnapshot, src: ConfigSnapshot) -> None:
        dst.credentials.clear()
        dst.credentials.update(src.credentials)
        dst.rate_rules.clear()
        dst.rate_rules.update(src.rate_rules)
        dst.flags.clear()
        dst.flags.update(src.flags)

    @staticmethod
    def _require_identifier(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value:
            raise ApiError(
                ErrorCode.INVALID_CONFIG, f"{field_name} must be a non-empty string"
            )
        return value

    @staticmethod
    def _validate_scopes(scopes) -> frozenset[str]:
        if not isinstance(scopes, (set, frozenset)) or not scopes:
            raise ApiError(
                ErrorCode.INVALID_CONFIG, "scopes must be a non-empty set"
            )
        scope_set = frozenset(scopes)
        unknown = scope_set - _VALID_SCOPES
        if unknown:
            raise ApiError(
                ErrorCode.INVALID_CONFIG,
                f"unknown scopes: {sorted(unknown)}",
                details={"allowed_scopes": sorted(_VALID_SCOPES)},
            )
        return scope_set

    @staticmethod
    def _validate_secret(secret: str) -> None:
        if not isinstance(secret, str) or len(secret) < 8:
            raise ApiError(
                ErrorCode.INVALID_CONFIG, "secret must be a string of at least 8 chars"
            )

    @staticmethod
    def _validate_expires(expires_at: float | None) -> None:
        if expires_at is not None and not isinstance(expires_at, (int, float)):
            raise ApiError(
                ErrorCode.INVALID_CONFIG, "expires_at must be epoch seconds or null"
            )
