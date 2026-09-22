"""灰度/功能开关。

- 开关取值在请求开始时快照一次，整个请求（含多步处理/重试点）一致；
- 配置热切换对进行中的请求无影响（配置整体原子替换，无半切换）；
- 非法配置在替换前被拒绝；运行期任何缺项回退到安全默认值（关闭）。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Settings
from app.constants import (
    FLAG_INCLUDE_METADATA,
    FLAG_INSIGHTS,
    SAFE_DEFAULTS,
)

_snapshot_var: contextvars.ContextVar[FeatureSnapshot | None] = contextvars.ContextVar(
    "feature_snapshot", default=None
)


class FeatureSnapshot:
    """请求级不可变开关视图。

    取值优先级：租户显式配置 -> 全局默认 -> 安全默认（关闭）。
    解析在构造时一次性完成，之后即使配置存储被热替换也不受影响。
    """

    def __init__(self, values: dict[str, bool], tenant_id: str) -> None:
        self._values = dict(values)
        self.tenant_id = tenant_id

    def get(self, name: str) -> bool:
        """缺项/未知开关一律回退安全默认值（关闭）。"""
        value = self._values.get(name, SAFE_DEFAULTS.get(name, False))
        return bool(value)

    def as_dict(self) -> dict[str, bool]:
        # 输出完整的已知开关视图，便于日志与管理接口解释判定依据
        merged = dict(SAFE_DEFAULTS)
        merged.update(self._values)
        return dict(sorted(merged.items()))


def build_snapshot(settings: Settings, tenant_id: str) -> FeatureSnapshot:
    """从配置快照构造本请求的开关快照。"""
    values = dict(SAFE_DEFAULTS)
    values.update(settings.default_flags)
    tenant = settings.tenants.get(tenant_id)
    if tenant is not None:
        values.update(tenant.flags)
    return FeatureSnapshot(values=values, tenant_id=tenant_id)


@contextmanager
def request_scope(snapshot: FeatureSnapshot) -> Iterator[None]:
    """把开关快照绑定到当前上下文（ContextVar 自动随请求隔离，退出即恢复）。"""
    token = _snapshot_var.set(snapshot)
    try:
        yield
    finally:
        _snapshot_var.reset(token)


def current_snapshot() -> FeatureSnapshot:
    """读取当前请求绑定的开关快照（无上下文说明装配错误）。"""
    snap = _snapshot_var.get()
    if snap is None:
        from app.errors import APIError, ErrorCode

        raise APIError(
            ErrorCode.INTERNAL_ERROR,
            500,
            "开关上下文未初始化",
        )
    return snap
