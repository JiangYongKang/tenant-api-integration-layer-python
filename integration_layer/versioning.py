"""接口版本注册与兼容规则。

规则：
- 多版本同时对外服务；请求必须显式声明版本。
- 字段新增（可选字段）向后兼容：旧版本请求在新版本处理器上可运行，响应按请求版本裁剪。
- 语义变更（字段重命名 / 必填化 / 类型变化）不兼容：只能出现在新主版本，旧版本行为冻结。
- 不存在的版本 -> UNSUPPORTED_VERSION；版本不支持的能力 -> UNSUPPORTED_CAPABILITY；
  对旧版本发起不兼容的变更请求 -> INCOMPATIBLE_CHANGE。绝不静默降级。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ApiError, ErrorCode


@dataclass(frozen=True)
class VersionSpec:
    version: str
    # 该版本请求体中已知的字段（含可选）；未知字段被拒绝而非忽略
    known_fields: frozenset[str]
    required_fields: frozenset[str]
    # 该版本支持的能力标记（如 "extended_status"）
    capabilities: frozenset[str] = frozenset()
    # 响应字段在各版本间的命名映射：{内部字段: 该版本对外字段名}
    response_field_names: dict[str, str] = field(default_factory=dict)


class VersionRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, VersionSpec] = {}

    def register(self, spec: VersionSpec) -> None:
        self._specs[spec.version] = spec

    def get(self, version: str | None) -> VersionSpec:
        if not version or version not in self._specs:
            raise ApiError(
                ErrorCode.UNSUPPORTED_VERSION,
                f"unsupported API version: {version!r}",
                details={"supported_versions": sorted(self._specs)},
            )
        return self._specs[version]

    @staticmethod
    def validate_payload(spec: VersionSpec, payload: dict[str, Any]) -> None:
        """按版本规则校验请求体：缺必填、含未知字段均明确拒绝。"""
        missing = spec.required_fields - payload.keys()
        if missing:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f"missing required fields: {sorted(missing)}",
                details={"missing": sorted(missing), "version": spec.version},
            )
        unknown = set(payload) - spec.known_fields
        if unknown:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f"unknown fields for version {spec.version}: {sorted(unknown)}",
                details={"unknown": sorted(unknown), "version": spec.version},
            )

    @staticmethod
    def require_capability(spec: VersionSpec, capability: str) -> None:
        if capability not in spec.capabilities:
            raise ApiError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"capability '{capability}' not available in version {spec.version}",
                details={"capability": capability, "version": spec.version},
            )

    @staticmethod
    def project_response(spec: VersionSpec, internal: dict[str, Any]) -> dict[str, Any]:
        """把内部表示投影为请求版本的对外表示（字段重命名 / 裁剪）。"""
        out: dict[str, Any] = {}
        for key, value in internal.items():
            out_key = spec.response_field_names.get(key, key)
            if out_key in spec.known_fields or out_key in (
                "id",
                "status",
                "status_detail",
                "created_at",
            ):
                out[out_key] = value
        return out
