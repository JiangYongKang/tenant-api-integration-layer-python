"""请求体上限、统一错误契约、内部异常屏蔽、请求标识贯穿。"""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from integration_layer.app import LayerConfig, create_app
from integration_layer.errors import ErrorCode

from .conftest import log_case


@pytest.fixture()
def small_app():
    return create_app(LayerConfig(max_body_bytes=256))


@pytest.fixture()
def small_client(small_app):
    return TestClient(small_app, raise_server_exceptions=False)


def test_oversized_body_rejected_by_content_length(small_client, small_app):
    key_id, secret = small_app.state.credentials.register("t", {"resources:write"})
    headers = {
        "x-api-key-id": key_id,
        "x-api-secret": secret,
        "x-api-version": "v1",
        "idempotency-key": "big-1",
    }
    payload = {"name": "x" * 1000}
    r = small_client.post("/api/resources", json=payload, headers=headers)
    log_case(
        "oversized body",
        {"max_body_bytes": 256, "sent": len(json.dumps(payload))},
        "超限请求体应稳定拒绝为 PAYLOAD_TOO_LARGE(413)",
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value
    assert r.json()["error"]["details"]["max_body_bytes"] == 256


def test_oversized_body_without_content_length(small_client, small_app):
    key_id, secret = small_app.state.credentials.register("t2", {"resources:write"})
    headers = {
        "x-api-key-id": key_id,
        "x-api-secret": secret,
        "x-api-version": "v1",
        "idempotency-key": "big-2",
        "content-type": "application/json",
    }
    body = b'{"name": "' + b"y" * 1000 + b'"}'
    # 流式发送，无 Content-Length，读取时兜底拦截
    r = small_client.post(
        "/api/resources",
        content=iter([body]),
        headers={k: v for k, v in headers.items()},
    )
    log_case(
        "oversized chunked body",
        {"max_body_bytes": 256, "sent": len(body), "content-length": "absent"},
        "无 Content-Length 时按实际读取字节数拦截，不无界占用内存",
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value


def test_internal_error_masked(app, client, tenant, caplog):
    _, headers = tenant

    # 注入一个会抛内部异常的副作用，验证对外不泄露原文/堆栈
    def boom(*a, **k):
        raise RuntimeError("secret internal path /srv/app/db.py line 42")

    app.state.resources.create = boom
    r = client.post(
        "/api/resources",
        json={"name": "n"},
        headers={**headers, "idempotency-key": "err-1"},
    )
    log_case(
        "internal error masking",
        {"injected": "RuntimeError with internal path"},
        "内部异常对外统一为 INTERNAL_ERROR，不含原文/堆栈/路径",
    )
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == ErrorCode.INTERNAL_ERROR.value
    assert "db.py" not in r.text
    assert "RuntimeError" not in r.text
    assert "Traceback" not in r.text


def test_failed_side_effect_leaves_no_residue(app, client, tenant):
    tenant_id, headers = tenant

    def boom(*a, **k):
        raise RuntimeError("transient")

    app.state.resources.create = boom
    r1 = client.post(
        "/api/resources", json={"name": "n"}, headers={**headers, "idempotency-key": "err-2"}
    )
    assert r1.status_code == 500
    # 恢复后同键重试应成功（失败不留残留幂等记录）
    app.state.resources.create = app.state.resources.__class__.create.__get__(
        app.state.resources
    )
    from integration_layer.resources import ResourceStore

    app.state.resources = ResourceStore()
    r2 = client.post(
        "/api/resources", json={"name": "n"}, headers={**headers, "idempotency-key": "err-2"}
    )
    log_case(
        "no residue after failure",
        {"first": "500", "retry": "same key"},
        "副作用失败后同键重试应能成功，无状态残留",
    )
    assert r2.status_code == 201


def test_request_id_propagates(client, tenant, caplog):
    _, headers = tenant
    with caplog.at_level(logging.INFO, logger="integration_layer"):
        r = client.post(
            "/api/resources",
            json={"name": "n"},
            headers={**headers, "idempotency-key": "rid-1", "x-request-id": "req-abc-123"},
        )
    log_case(
        "request id propagation",
        {"x-request-id": "req-abc-123"},
        "请求标识需出现在响应头、响应体与日志中",
    )
    assert r.status_code == 201
    assert r.headers["x-request-id"] == "req-abc-123"
    assert r.json()["request_id"] == "req-abc-123"
    assert any("req-abc-123" in rec.getMessage() for rec in caplog.records)


def test_error_contract_shape(client):
    r = client.post("/api/resources", json={"name": "x"})
    log_case("error contract", {"auth": "missing"}, "错误响应为稳定机器可读结构 error.code/message/request_id")
    body = r.json()
    assert set(body) == {"error"}
    assert {"code", "message", "request_id"} <= set(body["error"])
