"""业务服务层：版本化的资源操作与响应整形。

v1：创建 widget（name 必填，data 可选），响应含 created_at（Unix 秒）。
v2：v1 全部能力 + 可选请求字段 channel + 响应增加 created_at_iso、
    可选 metadata（受开关 widgets.include_metadata 控制）+
    v2 独有能力 GET /widgets/{id}/insights（受开关 widgets.insights 控制）。

兼容性：
- v2 新增字段全部可选，旧客户端无需改动（新增向后兼容）；
- v1 请求携带 v2 独有字段 -> incompatible_request（拒绝静默丢弃）；
- v1 的 created_at 语义在 v2 保持不变，仅新增 ISO 字段（语义不变更）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.auth import Principal
from app.constants import FLAG_INCLUDE_METADATA, FLAG_INSIGHTS
from app.errors import APIError, ErrorCode
from app.feature_flags import FeatureSnapshot
from app.stores import ResourceStore
from app.versioning import V2_ONLY_REQUEST_FIELDS, reject_version_only_fields

MAX_NAME_LEN = 200
MAX_CHANNEL_LEN = 64
MAX_DATA_KEYS = 50

# v1 允许的全部请求字段；出现其他字段视为非法（严格模式，避免静默忽略）
V1_ALLOWED_FIELDS = frozenset({"name", "data"})
V2_ALLOWED_FIELDS = V1_ALLOWED_FIELDS | V2_ONLY_REQUEST_FIELDS


class CreateWidgetInput:
    __slots__ = ("name", "data", "channel")

    def __init__(self, name: str, data: dict[str, Any] | None, channel: str | None) -> None:
        self.name = name
        self.data = data
        self.channel = channel


def _fail(message: str, field: str, rule: str) -> None:
    raise APIError(
        ErrorCode.VALIDATION_FAILED,
        422,
        message,
        details={"fields": [{"field": field, "rule": rule}]},
    )


def validate_widget_input(version: int, raw: dict[str, Any]) -> CreateWidgetInput:
    """按版本语义校验请求体（类型/长度/未知字段/超限输入）。"""
    if not isinstance(raw, dict):
        _fail("请求体必须是 JSON 对象", "body", "type_object")

    fields = set(raw.keys())
    # 1) 旧版本携带新版本独有字段：不可兼容变更，明确拒绝
    reject_version_only_fields(version, fields)
    # 2) 严格未知字段检查（不静默忽略调用方输入）
    allowed = V2_ALLOWED_FIELDS if version >= 2 else V1_ALLOWED_FIELDS
    unknown = fields - allowed
    if unknown:
        raise APIError(
            ErrorCode.VALIDATION_FAILED,
            422,
            f"存在未知字段 {sorted(unknown)}，请按对应版本的字段契约发送",
            details={"fields": [{"field": f, "rule": "unknown_field"} for f in sorted(unknown)]},
        )

    name = raw.get("name")
    if not isinstance(name, str):
        _fail("name 为必填字符串", "name", "required_string")
    if not (1 <= len(name) <= MAX_NAME_LEN):
        _fail(f"name 长度必须在 1-{MAX_NAME_LEN} 字符之间", "name", "length")

    data = raw.get("data")
    if data is not None:
        if not isinstance(data, dict):
            _fail("data 必须是 JSON 对象", "data", "type_object")
        if len(data) > MAX_DATA_KEYS:
            _fail(f"data 最多包含 {MAX_DATA_KEYS} 个键", "data", "max_keys")
        for k, v in data.items():
            if not isinstance(k, str):
                _fail("data 的键必须是字符串", "data", "key_type")
            if isinstance(v, str) and len(v) > 4096:
                _fail("data 中单个字符串值不得超过 4096 字符", f"data.{k}", "max_length")

    channel: str | None = None
    if version >= 2:
        channel = raw.get("channel")
        if channel is not None:
            if not isinstance(channel, str):
                _fail("channel 必须是字符串", "channel", "type_string")
            if not (1 <= len(channel) <= MAX_CHANNEL_LEN):
                _fail(f"channel 长度必须在 1-{MAX_CHANNEL_LEN} 字符之间", "channel", "length")

    return CreateWidgetInput(name=name, data=data, channel=channel)


def _shape_response(
    stored: dict[str, Any],
    version: int,
    flags: FeatureSnapshot,
) -> dict[str, Any]:
    """按版本整形响应；新版本只做"加法"，绝不改变 v1 既有字段语义。"""
    body: dict[str, Any] = {
        "id": stored["id"],
        "name": stored["name"],
        "created_at": stored["created_at"],  # Unix 秒，v1/v2 语义一致
        "tenant_id": stored["tenant_id"],
    }
    if stored.get("data") is not None:
        body["data"] = stored["data"]
    if version >= 2:
        body["created_at_iso"] = stored["created_at_iso"]
        body["channel"] = stored.get("channel")  # v2 新增可选字段，缺省 null
        if flags.get(FLAG_INCLUDE_METADATA):
            body["metadata"] = {
                "api_version": 2,
                "channel": stored.get("channel"),
            }
    return body


def create_widget(
    resources: ResourceStore,
    principal: Principal,
    flags: FeatureSnapshot,
    version: int,
    inp: CreateWidgetInput,
    now_ms: int,
) -> dict[str, Any]:
    """产生一次副作用并按版本整形响应。"""
    created_at_sec = now_ms // 1000
    created_iso = datetime.fromtimestamp(created_at_sec, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    item = {
        "name": inp.name,
        "data": inp.data,
        "channel": inp.channel if version >= 2 else None,
        "tenant_id": principal.tenant_id,
        "created_at": created_at_sec,
        "created_at_iso": created_iso,
    }
    stored = resources.create(item)
    return _shape_response(stored, version, flags)


def _ensure_owned(stored: dict[str, Any], principal: Principal) -> dict[str, Any]:
    if stored["tenant_id"] != principal.tenant_id:
        # 对非属主统一呈现"不存在"，不泄露资源是否存在
        raise APIError(ErrorCode.VALIDATION_FAILED, 404, "资源不存在", details={})
    return stored


def get_widget(
    resources: ResourceStore,
    principal: Principal,
    flags: FeatureSnapshot,
    version: int,
    item_id: str,
) -> dict[str, Any]:
    """读取资源并按版本整形（租户隔离）。"""
    stored = resources.get(item_id)
    if stored is None:
        raise APIError(ErrorCode.VALIDATION_FAILED, 404, "资源不存在", details={})
    _ensure_owned(stored, principal)
    return _shape_response(stored, version, flags)


def get_insights(
    resources: ResourceStore,
    principal: Principal,
    item_id: str,
    flags: FeatureSnapshot,
) -> dict[str, Any]:
    """v2 独有能力：返回资源统计（开关关闭时由路由层以 unsupported_capability 拒绝）。"""
    if not flags.get(FLAG_INSIGHTS):
        raise APIError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            404,
            "该能力（widgets.insights）未对当前租户开放或已被开关关闭",
            details={"capability": "widgets.insights"},
        )
    stored = resources.get(item_id)
    if stored is None:
        raise APIError(ErrorCode.VALIDATION_FAILED, 404, "资源不存在", details={})
    _ensure_owned(stored, principal)
    return {
        "id": stored["id"],
        "insights": {
            "name_length": len(stored["name"]),
            "has_data": stored.get("data") is not None,
            "channel": stored.get("channel"),
        },
    }
