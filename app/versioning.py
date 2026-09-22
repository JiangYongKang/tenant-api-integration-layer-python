"""接口版本协商与兼容性规则。

- 版本号写在路径前缀：``/api/v1/...``、``/api/v2/...``；
- ``Accept-Version`` 头可用于声明调用方期望版本，仅做一致性校验；
- 字段新增（v2 新增可选字段 ``channel``、``created_at_iso``）向后兼容；
- 向旧版本传入新版本独有字段属于不可兼容变更，必须拒绝；
- 版本独有能力（v2 insights）在旧版本上必须以 ``unsupported_capability`` 拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.errors import APIError, ErrorCode

SUPPORTED_VERSIONS: frozenset[int] = frozenset({1, 2})
DEFAULT_VERSION = 1
LATEST_VERSION = 2

# v2 相对 v1 新增的、仅属于 v2 的请求字段
V2_ONLY_REQUEST_FIELDS: frozenset[str] = frozenset({"channel"})


@dataclass(frozen=True)
class VersionInfo:
    version: int


def parse_path_prefix(prefix: str) -> int:
    """从 ``v1``/``v2`` 形式解析版本号，非法时抛 VERSION_NOT_FOUND。"""
    if not prefix.startswith("v"):
        raise APIError(
            ErrorCode.VERSION_NOT_FOUND,
            404,
            f"不存在的接口版本: {prefix!r}，支持版本: {sorted(SUPPORTED_VERSIONS)}",
            details={"requested_version": prefix},
        )
    try:
        num = int(prefix[1:])
    except (ValueError, IndexError):
        raise APIError(
            ErrorCode.VERSION_NOT_FOUND,
            404,
            f"不存在的接口版本: {prefix!r}",
            details={"requested_version": prefix},
        ) from None
    if num not in SUPPORTED_VERSIONS:
        raise APIError(
            ErrorCode.VERSION_NOT_FOUND,
            404,
            f"接口版本 v{num} 不受支持，支持版本: {sorted(SUPPORTED_VERSIONS)}",
            details={"requested_version": f"v{num}", "supported": sorted(SUPPORTED_VERSIONS)},
        )
    return num


def check_accept_header(header_value: str | None, path_version: int) -> None:
    """Accept-Version 与路径版本冲突时抛 INCOMPATIBLE_REQUEST，否则通过。

    支持的取值：``v2`` / ``2``；缺失表示不声明（以路径为准）。
    """
    if header_value is None or header_value.strip() == "":
        return
    raw = header_value.strip().lower().removeprefix("v")
    try:
        wanted = int(raw)
    except ValueError:
        raise APIError(
            ErrorCode.VALIDATION_FAILED,
            400,
            f"无法识别的 Accept-Version 取值: {header_value!r}",
            details={"header": "Accept-Version", "value": header_value},
        ) from None
    if wanted not in SUPPORTED_VERSIONS:
        raise APIError(
            ErrorCode.VERSION_NOT_FOUND,
            404,
            f"Accept-Version 声明了不受支持的版本: v{wanted}",
            details={"requested_version": f"v{wanted}", "supported": sorted(SUPPORTED_VERSIONS)},
        )
    if wanted != path_version:
        raise APIError(
            ErrorCode.INCOMPATIBLE_REQUEST,
            400,
            f"路径版本 v{path_version} 与 Accept-Version: v{wanted} 不一致，拒绝以避免语义歧义",
            details={"path_version": path_version, "accept_version": wanted},
        )


def reject_version_only_fields(version: int, body_fields: set[str]) -> None:
    """旧版本请求体中出现新版本独有字段时抛 INCOMPATIBLE_REQUEST。

    选择拒绝而非静默丢弃：静默忽略调用方显式传入的字段会造成
    "以为生效、实际未生效"的不可解释结果。
    """
    if version >= LATEST_VERSION:
        return
    forbidden = body_fields & V2_ONLY_REQUEST_FIELDS
    if forbidden:
        raise APIError(
            ErrorCode.INCOMPATIBLE_REQUEST,
            400,
            f"字段(s) {sorted(forbidden)} 仅在 v2 及以上版本受支持，"
            "当前请求为旧版本且属于不可兼容变更，请改用 /api/v2 或移除该字段",
            details={
                "unsupported_fields": sorted(forbidden),
                "since_version": 2,
                "request_version": version,
            },
        )
