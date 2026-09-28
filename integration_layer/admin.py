"""仅本机可用的运维入口：凭证签发/轮换/吊销、限流调整、灰度调整、配置版本与回退。

安全约定：
- 仅接受来自 loopback（127.0.0.1 / ::1 / testclient）的请求，其他来源一律 404
  （对外部不暴露入口存在）。
- 任何响应与日志都不回显完整密钥，只给可核对的脱敏串与不可逆指纹。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config_manager import ConfigManager, mask_secret, secret_fingerprint
from .errors import ApiError, ErrorCode
from .feature_flags import FlagRule
from .rate_limit import RateLimitRule

logger = logging.getLogger("integration_layer.admin")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


def register_admin_routes(app: FastAPI) -> None:
    def _manager(request: Request) -> ConfigManager:
        return request.app.state.config_manager

    def _require_local(request: Request) -> None:
        host = (request.client.host if request.client else "") or ""
        if host not in _LOOPBACK_HOSTS:
            # 对外不暴露运维入口：统一 404，与“资源不存在”无差别
            raise ApiError(ErrorCode.NOT_FOUND, "not found")

    def _admin_body(request: Request) -> dict[str, Any]:
        import json

        raw = getattr(request.state, "raw_body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(ErrorCode.VALIDATION_FAILED, "request body is not valid JSON")
        if not isinstance(data, dict):
            raise ApiError(ErrorCode.VALIDATION_FAILED, "request body must be a JSON object")
        return data

    def _ok(request: Request, payload: dict[str, Any]) -> JSONResponse:
        payload["config_version"] = _manager(request).current_version
        rid = request.state.request_id
        return JSONResponse(content=payload, headers={"x-request-id": rid})

    def _credential_view(
        key_id: str,
        secret: str,
        tenant_id: str | None = None,
        scopes: Any = None,
    ) -> dict[str, Any]:
        """凭证对外视图：只含脱敏串与不可逆指纹，绝不含完整密钥。"""
        view = {
            "key_id": key_id,
            "secret_masked": mask_secret(secret),
            "secret_fingerprint": secret_fingerprint(secret),
        }
        if tenant_id is not None:
            view["tenant_id"] = tenant_id
        if scopes is not None:
            view["scopes"] = sorted(scopes)
        return view

    # ---- 凭证 ----

    @app.post("/admin/credentials")
    async def admin_issue_credential(request: Request) -> JSONResponse:
        _require_local(request)
        data = _admin_body(request)
        tenant_id = data.get("tenant_id")
        scopes = data.get("scopes")
        if not isinstance(scopes, list):
            raise ApiError(
                ErrorCode.INVALID_CONFIG, "scopes must be a non-empty list"
            )
        secret = data.get("secret")  # 可选：运维自行提供密钥
        expires_at = data.get("expires_at")
        manager = _manager(request)
        key_id, issued = manager.issue_credential(
            tenant_id, set(scopes), secret=secret, expires_at=expires_at
        )
        logger.info(
            "event=credential_issued request_id=%s key_id=%s fp=%s",
            request.state.request_id,
            key_id,
            secret_fingerprint(issued),
        )
        return _ok(
            request,
            {
                "credential": _credential_view(key_id, issued, tenant_id, scopes),
                "note": "store the secret now; it is never shown again",
            },
        )

    @app.post("/admin/credentials/{key_id}/rotate")
    async def admin_rotate_credential(key_id: str, request: Request) -> JSONResponse:
        _require_local(request)
        data = _admin_body(request)
        secret = data.get("secret")
        expires_at = data.get("expires_at")
        manager = _manager(request)
        old = manager.snapshot().credentials.get(key_id)
        tenant_id = old.tenant_id if old else None
        new_key_id, issued = manager.rotate_credential(
            key_id, secret=secret, expires_at=expires_at
        )
        scopes = manager.snapshot().credentials[new_key_id].scopes
        logger.info(
            "event=credential_rotated request_id=%s old_key_id=%s new_key_id=%s fp=%s",
            request.state.request_id,
            key_id,
            new_key_id,
            secret_fingerprint(issued),
        )
        return _ok(
            request,
            {
                "revoked_key_id": key_id,
                "credential": _credential_view(new_key_id, issued, tenant_id, scopes),
            },
        )

    @app.post("/admin/credentials/{key_id}/revoke")
    async def admin_revoke_credential(key_id: str, request: Request) -> JSONResponse:
        _require_local(request)
        manager = _manager(request)
        existed = manager.revoke_credential(key_id)
        logger.info(
            "event=credential_revoked request_id=%s key_id=%s existed=%s",
            request.state.request_id,
            key_id,
            existed,
        )
        # 吊销幂等：重复吊销给出一致、可解释的结果
        return _ok(request, {"key_id": key_id, "revoked": True, "existed": existed})

    # ---- 限流 ----

    @app.put("/admin/rate-limits/{tenant_id}")
    async def admin_set_rate_limit(tenant_id: str, request: Request) -> JSONResponse:
        _require_local(request)
        data = _admin_body(request)
        endpoint = data.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ApiError(
                ErrorCode.INVALID_CONFIG,
                "endpoint must be a non-empty string, e.g. 'POST /api/resources'",
            )
        capacity = data.get("capacity")
        refill = data.get("refill_per_second")
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise ApiError(
                ErrorCode.INVALID_CONFIG, "capacity must be a positive integer"
            )
        if not isinstance(refill, (int, float)) or isinstance(refill, bool):
            raise ApiError(
                ErrorCode.INVALID_CONFIG,
                "refill_per_second must be a positive number",
            )
        rule = RateLimitRule(capacity=capacity, refill_per_second=float(refill))
        manager = _manager(request)
        version = manager.set_rate_rule(tenant_id, endpoint, rule)
        # 规则已在配置快照内原子生效；仅重置对应令牌桶（不再产生第二次配置提交）
        request.app.state.rate_limiter.reset_bucket(tenant_id, endpoint)
        logger.info(
            "event=rate_limit_updated request_id=%s tenant=%s endpoint=%s v=%s",
            request.state.request_id,
            tenant_id,
            endpoint,
            version,
        )
        return _ok(
            request,
            {
                "tenant_id": tenant_id,
                "endpoint": endpoint,
                "capacity": capacity,
                "refill_per_second": float(refill),
            },
        )

    # ---- 灰度 ----

    @app.put("/admin/flags/{flag_name}")
    async def admin_set_flag(flag_name: str, request: Request) -> JSONResponse:
        _require_local(request)
        data = _admin_body(request)
        rollout = data.get("rollout", 0)
        overrides = data.get("overrides", {})
        rule = FlagRule(rollout=rollout, overrides=overrides)  # 非法在此抛 INVALID_CONFIG
        manager = _manager(request)
        version = manager.set_flag(flag_name, rule)
        logger.info(
            "event=flag_updated request_id=%s flag=%s rollout=%s v=%s",
            request.state.request_id,
            flag_name,
            rollout,
            version,
        )
        return _ok(
            request,
            {"flag": flag_name, "rollout": rollout, "overrides": dict(overrides)},
        )

    @app.put("/admin/flags")
    async def admin_replace_flags(request: Request) -> JSONResponse:
        """整体替换全部灰度开关（原子生效）。"""
        _require_local(request)
        data = _admin_body(request)
        raw_flags = data.get("flags")
        if not isinstance(raw_flags, dict):
            raise ApiError(ErrorCode.INVALID_CONFIG, "flags must be an object")
        flags: dict[str, FlagRule] = {}
        for name, cfg in raw_flags.items():
            if not isinstance(cfg, dict):
                raise ApiError(
                    ErrorCode.INVALID_CONFIG, f"flag '{name}' config must be an object"
                )
            flags[name] = FlagRule(
                rollout=cfg.get("rollout", 0),
                overrides=cfg.get("overrides", {}),
            )
        manager = _manager(request)
        version = manager.replace_flags(flags)
        logger.info(
            "event=flags_replaced request_id=%s flags=%s v=%s",
            request.state.request_id,
            sorted(flags),
            version,
        )
        return _ok(request, {"flags": sorted(flags)})

    # ---- 配置版本与回退 ----

    @app.get("/admin/config/version")
    async def admin_config_version(request: Request) -> JSONResponse:
        _require_local(request)
        manager = _manager(request)
        return _ok(
            request,
            {
                "current_version": manager.current_version,
                "history_versions": manager.history_versions(),
            },
        )

    @app.post("/admin/config/rollback")
    async def admin_rollback(request: Request) -> JSONResponse:
        _require_local(request)
        data = _admin_body(request)
        target = data.get("version")
        if not isinstance(target, int) or isinstance(target, bool):
            # 入参形态非法属于参数错误，与“版本不存在”区分
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "version must be an integer"
            )
        manager = _manager(request)
        manager.rollback(target)  # 目标不存在 -> CONFIG_VERSION_NOT_FOUND(404)
        # 回退整体生效：清空令牌桶，使限流行为与目标版本一致（无桶状态残留）
        request.app.state.rate_limiter.reset_all_buckets()
        logger.info(
            "event=config_rolled_back request_id=%s target=%s now=%s",
            request.state.request_id,
            target,
            manager.current_version,
        )
        return _ok(
            request,
            {"rolled_back_from": target, "current_version": manager.current_version},
        )
