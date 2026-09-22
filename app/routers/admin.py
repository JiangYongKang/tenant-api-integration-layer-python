"""本地管理路由：健康检查、请求 ID 回显、配置查看与原子热切换。

- GET  /healthz                 无需鉴权，仅活性探测
- GET  /_local/whoami           回显当前租户/权限/开关视图与 request_id
- GET  /_local/config           需要 config:read；回显脱敏配置
- POST /_local/config/replace   需要 config:write；整体校验后原子替换
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth import authenticate, require_scope
from app.config import ConfigError, to_json, validate
from app.dependencies import Deps
from app.feature_flags import build_snapshot
from app.logging_setup import config_version_var, log_event, tenant_id_var
from app.errors import APIError, ErrorCode


def register(app: FastAPI, deps: Deps) -> None:
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/_local/whoami")
    async def whoami(request: Request) -> JSONResponse:
        settings = deps.snapshot_settings()
        principal = authenticate(
            settings,
            request.headers.get("authorization"),
            deps.clock_ms() // 1000,
            logger=deps.logger,
        )
        tenant_id_var.set(principal.tenant_id)
        config_version_var.set(settings.config_version)
        flags = build_snapshot(settings, principal.tenant_id)
        return JSONResponse(
            status_code=200,
            content={
                "request_id": request.state.request_id,
                "tenant_id": principal.tenant_id,
                "credential_label": principal.label,
                "scopes": sorted(principal.scopes),
                "config_version": settings.config_version,
                "flags": flags.as_dict(),
            },
            headers={"X-Request-Id": request.state.request_id},
        )

    @app.get("/_local/config")
    async def get_config(request: Request) -> JSONResponse:
        settings = deps.snapshot_settings()
        principal = authenticate(
            settings,
            request.headers.get("authorization"),
            deps.clock_ms() // 1000,
            logger=deps.logger,
        )
        require_scope(principal, "config:read")
        tenant_id_var.set(principal.tenant_id)
        config_version_var.set(settings.config_version)
        return JSONResponse(
            status_code=200,
            content=json_safe(settings),
            headers={"X-Request-Id": request.state.request_id},
        )

    @app.post("/_local/config/replace")
    async def replace_config(request: Request) -> JSONResponse:
        settings = deps.snapshot_settings()
        principal = authenticate(
            settings,
            request.headers.get("authorization"),
            deps.clock_ms() // 1000,
            logger=deps.logger,
        )
        require_scope(principal, "config:write")
        tenant_id_var.set(principal.tenant_id)
        config_version_var.set(settings.config_version)

        raw_body = await request.body()
        try:
            import json

            raw = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(
                ErrorCode.VALIDATION_FAILED,
                422,
                "配置必须是合法 UTF-8 JSON",
                details={"fields": [{"field": "body", "rule": "invalid_json"}]},
            ) from None

        try:
            next_settings = validate(raw)
        except ConfigError as exc:
            # 拒绝非法配置：旧配置保持不变（绝不半切换）
            log_event(
                deps.logger,
                "config_replace_rejected",
                decision="reject",
                tenant_id=principal.tenant_id,
                input_config_version=raw.get("config_version") if isinstance(raw, dict) else None,
                decision_error_path=exc.path,
                decision_reason=exc.reason,
            )
            raise APIError(
                ErrorCode.CONFIG_INVALID,
                422,
                "配置校验失败，已拒绝热切换并继续使用旧配置",
                details={"path": exc.path, "reason": exc.reason},
            ) from None

        new_version = deps.config_store.replace(next_settings)
        log_event(
            deps.logger,
            "config_replaced",
            decision="atomic_replace",
            tenant_id=principal.tenant_id,
            old_config_version=settings.config_version,
            new_config_version=new_version,
        )
        return JSONResponse(
            status_code=200,
            content={"old_config_version": settings.config_version, "config_version": new_version},
            headers={"X-Request-Id": request.state.request_id},
        )


def json_safe(settings: Any) -> dict[str, Any]:
    import json

    return json.loads(to_json(settings))
