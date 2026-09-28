"""灰度 / 开关配置：快照式读取，保证同一请求内取值一致；非法配置被拒绝或回退安全默认。

- 配置整体原子替换（不可变快照），运行中切换不会出现半切换状态。
- 灰度按 (tenant_id, flag) 稳定哈希分桶，同一租户对同一开关结果确定。
- 非法配置：更新时拒绝并保留旧配置；构造时无旧配置可回退则使用安全默认（全关）。
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from .config_manager import ConfigManager


@dataclass(frozen=True)
class FlagRule:
    """rollout 为 0..100 的整数百分比；overrides 为租户级强制取值。"""

    rollout: int = 0
    overrides: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.rollout, int) or not (0 <= self.rollout <= 100):
            raise ApiError(
                ErrorCode.INVALID_CONFIG,
                f"flag rollout must be an integer in [0, 100], got {self.rollout!r}",
            )
        for tenant, value in self.overrides.items():
            if not isinstance(value, bool):
                raise ApiError(
                    ErrorCode.INVALID_CONFIG,
                    f"flag override for tenant '{tenant}' must be boolean",
                )


@dataclass(frozen=True)
class FlagSnapshot:
    """不可变配置快照；请求开始时取一次，整个请求内一致。"""

    flags: dict[str, FlagRule]

    def is_enabled(self, flag: str, tenant_id: str) -> bool:
        rule = self.flags.get(flag)
        if rule is None:
            return False  # 未配置的开关安全默认关闭
        override = rule.overrides.get(tenant_id)
        if override is not None:
            return override
        if rule.rollout <= 0:
            return False
        if rule.rollout >= 100:
            return True
        bucket = int.from_bytes(
            hashlib.sha256(f"{flag}:{tenant_id}".encode("utf-8")).digest()[:4], "big"
        ) % 100
        return bucket < rule.rollout


class FlagStore:
    """线程安全的开关存储；replace 原子生效。"""

    def __init__(self, flags: dict[str, FlagRule] | None = None) -> None:
        self._lock = threading.Lock()
        self._snapshot: FlagSnapshot | None = None
        self._manager: "ConfigManager | None" = None
        try:
            self._snapshot = FlagSnapshot(flags=dict(flags or {}))
        except ApiError:
            self._snapshot = FlagSnapshot(flags={})  # 回退安全默认

    def bind(self, manager: "ConfigManager") -> None:
        """绑定配置管理器：读取以管理器快照为准；replace 委托原子提交。"""
        self._manager = manager

    def snapshot(self) -> FlagSnapshot:
        if self._manager is not None:
            return FlagSnapshot(flags=dict(self._manager.snapshot().flags))
        with self._lock:
            assert self._snapshot is not None
            return self._snapshot

    def replace(self, flags: dict[str, FlagRule]) -> None:
        """原子替换；非法配置抛 INVALID_CONFIG 且保留旧配置。"""
        if self._manager is not None:
            self._manager.replace_flags(flags)
            return
        new_snapshot = FlagSnapshot(flags=dict(flags))  # 校验失败则抛错，旧配置不动
        with self._lock:
            self._snapshot = new_snapshot
