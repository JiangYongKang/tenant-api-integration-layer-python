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
    """key = (tenant_id, endpoint)，各 key 独立计数，无跨租户串扰。"""

    def __init__(self, default_rule: RateLimitRule) -> None:
        self._lock = threading.Lock()
        self._default_rule = default_rule
        self._rules: dict[tuple[str, str], RateLimitRule] = {}
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def set_rule(self, tenant_id: str, endpoint: str, rule: RateLimitRule) -> None:
        with self._lock:
            self._rules[(tenant_id, endpoint)] = rule
            self._buckets.pop((tenant_id, endpoint), None)  # 配置变更重置桶，避免半切换

    def _rule_for(self, key: tuple[str, str]) -> RateLimitRule:
        return self._rules.get(key, self._default_rule)

    def check(self, tenant_id: str, endpoint: str, now: float | None = None) -> None:
        """消耗一个令牌；不足时以 RATE_LIMITED 拒绝并给出重试提示。"""
        now = time.monotonic() if now is None else now
        key = (tenant_id, endpoint)
        with self._lock:
            rule = self._rule_for(key)
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
