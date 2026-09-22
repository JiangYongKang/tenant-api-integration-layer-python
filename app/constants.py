"""跨模块共享的稳定常量（避免 config <-> feature_flags 循环依赖）。"""

from __future__ import annotations

# 功能开关名（对外稳定契约）
FLAG_INSIGHTS = "widgets.insights"
FLAG_INCLUDE_METADATA = "widgets.include_metadata"

# 安全默认值：任何新能力/缺项/未知项一律回退为关闭
SAFE_DEFAULTS: dict[str, bool] = {
    FLAG_INSIGHTS: False,
    FLAG_INCLUDE_METADATA: False,
}

# 权限范围白名单
KNOWN_SCOPES: frozenset[str] = frozenset(
    {"widgets:read", "widgets:write", "config:read", "config:write"}
)
