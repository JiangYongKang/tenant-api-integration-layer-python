"""按租户 + 接口维度的令牌桶限流，配置可更新，租户间完全隔离。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .errors import ApiError, ErrorCode


@dataclass(frozen=True)
class RateLimitRule:
    capacity: int  # 桶容量（突发上限）
    refill_per_second: float  # 匀速补充速率

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ApiError(
                ErrorCode.INVALID_CONFIG,
                "rate limit rule requires positive capacity and refill rate",
            )


class _Bucket:
    __slots__ = ("tokens", "updated_at")

    def __init__(self, capacity: int, now: float) -> None:
        self.tokens = float(capacity)
        self.updated_at = now


class RateLimiter:
    """key = (tenant_id, endpoint)，各 key 独立计数，无跨租户串扰。

    独立模式：规则来自构造默认值与 set_rule；
    绑定 ConfigManager 后：规则来自当前生效快照，set_rule 走版本化提交。
    令牌桶是瞬时运行状态，不参与配置快照/回退。
    """

    def __init__(self, default_rule: RateLimitRule) -> None:
        self._lock = threading.Lock()
        self._default_rule = default_rule
        self._rules: dict[tuple[str, str], RateLimitRule] = {}
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._active_rules: dict[tuple[str, str], RateLimitRule] = {}
        self._manager = None

    def bind(self, manager) -> None:
        self._manager = manager

    def set_rule(self, tenant_id: str, endpoint: str, rule: RateLimitRule) -> None:
        if self._manager is not None:
            # 管理器会校验并原子生效；桶按规则身份变化惰性重置
            self._manager.set_rate_rule(tenant_id, endpoint, rule)
            return
        with self._lock:
            self._rules[(tenant_id, endpoint)] = rule
            self._buckets.pop((tenant_id, endpoint), None)  # 配置变更重置桶，避免半切换

    def _rule_for(self, key: tuple[str, str]) -> RateLimitRule:
        return self._rules.get(key, self._default_rule)

    def check(self, tenant_id: str, endpoint: str, now: float | None = None,
              snapshot=None) -> None:
        """消耗一个令牌；不足时以 RATE_LIMITED 拒绝并给出重试提示。

        传入 snapshot 时规则取自该请求快照，保证请求内一致、不受切换/回退影响。
        """
        now = time.monotonic() if now is None else now
        key = (tenant_id, endpoint)
        with self._lock:
            if snapshot is not None:
                rule = snapshot.rate_rules.get(key, self._default_rule)
            elif self._manager is not None:
                rule = self._manager.current().rate_rules.get(key, self._default_rule)
            else:
                rule = self._rule_for(key)
            # 规则身份变化（含回退到旧规则）即重置桶，避免按旧速率累积的令牌泄漏
            if self._active_rules.get(key) is not rule:
                self._buckets.pop(key, None)
                self._active_rules[key] = rule
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(rule.capacity, now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(rule.capacity, bucket.tokens + elapsed * rule.refill_per_second)
            bucket.updated_at = now
            if bucket.tokens < 1.0:
                retry_after = (1.0 - bucket.tokens) / rule.refill_per_second
                raise ApiError(
                    ErrorCode.RATE_LIMITED,
                    "rate limit exceeded",
                    retry_after=round(retry_after, 3),
                )
            bucket.tokens -= 1.0
