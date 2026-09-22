"""结构化日志与请求标识。

请求 ID 贯穿日志与响应；凭证在日志中必须脱敏，禁止记录完整凭证。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import sys
import uuid
from typing import Any

# 透传当前请求上下文（request_id / tenant_id / config_version）
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_request_id", default=None
)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_tenant_id", default=None
)
config_version_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "log_config_version", default=None
)

# 兼容旧名字
context = request_id_var

SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid is not None:
            payload["request_id"] = rid
        tid = tenant_id_var.get()
        if tid is not None:
            payload["tenant_id"] = tid
        cv = config_version_var.get()
        if cv is not None:
            payload["config_version"] = cv
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in payload:
                    payload[k] = _safe(v)
        if record.exc_info:
            # 仅记录异常类型名与脱敏后的信息，不把内部原文送进业务响应；
            # 日志保留异常类型，便于排障，不包含请求中的敏感参数。
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe(v: Any, _key: str = "") -> Any:
    """日志字段清洗：禁止把疑似凭证的原值写出。

    1) 已知敏感头/键名整体打码；2) 字符串中形如 ``Bearer xxx`` 的片段打码。
    """
    if _key.lower() in SENSITIVE_HEADERS:
        return "***"
    if isinstance(v, dict):
        return {k: _safe(x, str(k)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe(x, _key) for x in v]
    if isinstance(v, str) and "bearer " in v.lower():
        import re

        return re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", v)
    return v


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """配置结构化 JSON 风格日志（单例），返回根 logger。"""
    root = logging.getLogger("tenant_api")
    if root.handlers:
        return root
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    return root


def new_request_id() -> str:
    """生成短请求标识（12 位 URL 安全随机串）。"""
    return uuid.uuid4().hex[:12]


def mask_credential(token: str | None) -> str:
    """凭证脱敏：仅保留可用于排障的极短指纹，绝不回显完整值。

    返回形如 ``sha256:ab12cd``（指纹前 6 位），无法反推原文。
    """
    if not token:
        return "none"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:6]}"


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """记录一条结构化事件（自动附带当前请求上下文）。

    调用方传入的输入参数建议放在 ``input_*`` 字段，判定依据放在
    ``decision_*`` 字段，便于测试日志核对"输入与判定依据"。
    """
    logger.info(event, extra={"fields": _safe(fields)})
