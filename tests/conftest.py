"""共享测试夹具：可控时钟、内存依赖、测试客户端、鉴权头助手。"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import build_default_settings
from app.config import Settings
from app.dependencies import build_deps
from app.logging_setup import configure_logging
from app.main import create_app

# 固定的"当前时间"：2026-09-22T00:00:00Z（秒/毫秒）
FIXED_NOW_SEC = 1_790_000_000
FIXED_NOW_MS = FIXED_NOW_SEC * 1000


class FakeClock:
    """可控毫秒时钟，支持测试中推进时间（限流窗口、过期判定）。"""

    def __init__(self, start_ms: int = FIXED_NOW_MS) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance_ms(self, delta_ms: int) -> None:
        self.now_ms += delta_ms


class ListHandler(logging.Handler):
    """收集结构化日志事件，供断言"输入与判定依据"已落日志。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        event: dict[str, Any] = {"event": record.getMessage(), "level": record.levelname}
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            event.update(fields)
        # request_id 由 formatter 从 ContextVar 注入，处理器阶段同样可读
        from app.logging_setup import request_id_var

        rid = request_id_var.get()
        if rid:
            event.setdefault("request_id", rid)
        self.events.append(event)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def env(clock: FakeClock):
    """构造全新的内存应用环境（每个测试独立，无状态残留）。"""
    logger = configure_logging()
    handler = ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    settings, tokens = build_default_settings(FIXED_NOW_SEC)
    deps = build_deps(settings, clock_ms=clock, logger=logger)
    app = create_app(deps)
    client = TestClient(app)

    class Env:
        def __init__(self) -> None:
            self.client = client
            self.deps = deps
            self.settings = settings
            self.tokens = tokens
            self.clock = clock
            self.handler = handler

        def auth(self, tenant: str, label: str) -> dict[str, str]:
            return {"Authorization": f"Bearer {self.tokens[tenant][label]}"}

        def raw_token(self, tenant: str, label: str) -> str:
            return self.tokens[tenant][label]

        def logs(self, event_name: str | None = None) -> list[dict[str, Any]]:
            if event_name is None:
                return list(self.handler.events)
            return [e for e in self.handler.events if e.get("event") == event_name]

        def settings_to_raw(self, s: Settings | None = None, version: int | None = None) -> dict[str, Any]:
            s = s or self.deps.config_store.current()
            return {
                "config_version": version if version is not None else s.config_version + 1,
                "default_flags": dict(s.default_flags),
                "default_max_body_bytes": s.default_max_body_bytes,
                "default_rate_limit": {
                    "limit": s.default_rate_limit.limit,
                    "window_ms": s.default_rate_limit.window_ms,
                },
                "tenants": {
                    tid: {
                        "scopes": sorted(t.scopes),
                        "credentials": [
                            {
                                "token_hash": c.token_hash,
                                "label": c.label,
                                "scopes": sorted(c.scopes),
                                "expires_at": c.expires_at,
                            }
                            for c in t.credentials
                        ],
                        "rate_limits": {
                            k: {"limit": rl.limit, "window_ms": rl.window_ms}
                            for k, rl in t.rate_limits.items()
                        },
                        "flags": dict(t.flags),
                        "max_body_bytes": t.max_body_bytes,
                    }
                    for tid, t in s.tenants.items()
                },
            }

    yield Env()
    logger.removeHandler(handler)


def create_widget(
    env,
    tenant: str = "tenant-a",
    label: str = "ci-writer",
    version: int = 1,
    key: str = "k-1",
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
):
    """发一次创建请求的便捷助手，返回 httpx Response。"""
    headers = env.auth(tenant, label)
    headers["Idempotency-Key"] = key
    if extra_headers:
        headers.update(extra_headers)
    return env.client.post(
        f"/api/v{version}/widgets",
        json=body if body is not None else {"name": "widget"},
        headers=headers,
    )


def assert_error(resp, status: int, code: str) -> dict[str, Any]:
    """断言统一错误契约并返回 error 对象。"""
    assert resp.status_code == status, resp.text
    data = resp.json()
    assert set(data.keys()) == {"error"}
    err = data["error"]
    assert err["code"] == code, err
    assert isinstance(err["message"], str) and err["message"]
    assert err["request_id"] == resp.headers["x-request-id"]
    return err
