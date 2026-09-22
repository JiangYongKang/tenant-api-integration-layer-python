"""鉴权与权限：缺失 / 无效 / 过期 / 权限不足必须可区分，且不回显凭证。"""
import time

from integration_layer.errors import ErrorCode

from .conftest import log_case


def test_missing_credential(client):
    r = client.post("/api/resources", json={"name": "x"}, headers={"x-api-version": "v1"})
    log_case("missing credential", {"headers": "none"}, "缺少 x-api-key-id/x-api-secret 应为 CREDENTIAL_MISSING")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == ErrorCode.CREDENTIAL_MISSING.value


def test_invalid_credential(client, tenant):
    _, headers = tenant
    bad = {**headers, "x-api-secret": "wrong-secret"}
    r = client.post("/api/resources", json={"name": "x"}, headers=bad)
    log_case("invalid credential", {"secret": "wrong-secret"}, "密钥不匹配应为 CREDENTIAL_INVALID")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == ErrorCode.CREDENTIAL_INVALID.value
    # 响应不得回显凭证
    assert "wrong-secret" not in r.text


def test_expired_credential(app, client):
    key_id, secret = app.state.credentials.register(
        "tenant-exp", {"resources:write"}, expires_at=time.time() - 1
    )
    headers = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1"}
    r = client.post("/api/resources", json={"name": "x"}, headers=headers)
    log_case("expired credential", {"expires_at": "past"}, "过期凭证应为 CREDENTIAL_EXPIRED")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == ErrorCode.CREDENTIAL_EXPIRED.value
    assert secret not in r.text


def test_permission_denied(app, client):
    key_id, secret = app.state.credentials.register("tenant-ro", {"resources:read"})
    headers = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1"}
    r = client.post("/api/resources", json={"name": "x"}, headers=headers)
    log_case("permission denied", {"scopes": ["resources:read"]}, "只读租户写入应为 PERMISSION_DENIED")
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == ErrorCode.PERMISSION_DENIED.value
    assert body["error"]["details"]["required_scope"] == "resources:write"
    assert secret not in r.text


def test_no_secret_leak_in_error_responses(client, tenant):
    _, headers = tenant
    secret = headers["x-api-secret"]
    # 触发多种错误，逐一确认不回显完整凭证
    triggers = [
        client.post("/api/resources", json={"name": "x"}, headers={**headers, "x-api-version": "v9"}),
        client.post("/api/resources", data=b"not-json", headers={**headers, "idempotency-key": "k"}),
        client.get("/api/resources/missing", headers=headers),
    ]
    for r in triggers:
        log_case("no secret leak", {"status": r.status_code}, "任何错误响应不得包含完整凭证")
        assert secret not in r.text
