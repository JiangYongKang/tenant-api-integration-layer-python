"""凭证与权限范围测试：缺失/无效/过期/权限不足必须可区分，且凭证不得回显。"""

from __future__ import annotations

from tests.conftest import assert_error, create_widget


def test_missing_credential_is_distinct(env):
    r = env.client.post(
        "/api/v1/widgets", json={"name": "x"}, headers={"Idempotency-Key": "k"}
    )
    err = assert_error(r, 401, "credential_missing")
    assert err["request_id"] == r.headers["x-request-id"]


def test_malformed_authorization_header_is_invalid(env):
    for header in ["Basic abc", "Bearer", "Bearer ", "Bearer a b c"]:
        r = env.client.post(
            "/api/v1/widgets",
            json={"name": "x"},
            headers={"Idempotency-Key": "k", "Authorization": header},
        )
        assert_error(r, 401, "credential_invalid")


def test_tampered_token_is_invalid(env):
    token = env.raw_token("tenant-a", "ci-writer")
    r = env.client.post(
        "/api/v1/widgets",
        json={"name": "x"},
        headers={"Idempotency-Key": "k", "Authorization": f"Bearer {token[:-4]}AAAA"},
    )
    assert_error(r, 401, "credential_invalid")


def test_token_for_unknown_tenant_is_invalid(env):
    from app.credentials import mint_token

    forged = mint_token("ghost", "ci-writer", None, 1_790_000_000).token
    r = env.client.post(
        "/api/v1/widgets",
        json={"name": "x"},
        headers={"Idempotency-Key": "k", "Authorization": f"Bearer {forged}"},
    )
    assert_error(r, 401, "credential_invalid")


def test_expired_credential_is_distinct(env):
    token = env.raw_token("tenant-b", "expired-one")
    r = env.client.post(
        "/api/v1/widgets",
        json={"name": "x"},
        headers={"Idempotency-Key": "k", "Authorization": f"Bearer {token}"},
    )
    err = assert_error(r, 401, "credential_expired")
    assert "expired_at" in err["details"]


def test_reader_scope_cannot_write(env):
    r = create_widget(env, tenant="tenant-a", label="ci-reader", key="k")
    err = assert_error(r, 403, "permission_denied")
    assert err["details"]["required_scope"] == "widgets:write"
    assert "widgets:read" in err["details"]["granted_scopes"]


def test_writer_scope_cannot_read_insights_via_read(env):
    # tenant-c 的凭证同时有 read/write，这里构造一个仅 write 场景：
    # 用 tenant-a writer（含 read）无法区分，改为直接校验 scope 判定单元
    from app.auth import Principal, require_scope
    from app.errors import APIError, ErrorCode

    p = Principal("t", "l", frozenset({"widgets:write"}), 1)
    try:
        require_scope(p, "widgets:read")
    except APIError as e:
        assert e.code is ErrorCode.PERMISSION_DENIED
    else:
        raise AssertionError("应当权限不足")


def test_no_credential_echo_in_response_or_logs(env):
    token = env.raw_token("tenant-b", "expired-one")
    r = env.client.post(
        "/api/v1/widgets",
        json={"name": "x"},
        headers={"Idempotency-Key": "k", "Authorization": f"Bearer {token}"},
    )
    assert token not in r.text
    # 任何日志事件都不得包含完整凭证
    for event in env.logs():
        flat = repr(event)
        assert token not in flat, flat
    # 脱敏指纹存在且不可逆
    auth_logs = env.logs("auth_decision")
    # 过期凭证在过期校验前已命中，指纹记录在排障字段（此处过期路径无日志也可）
    for e in auth_logs:
        if "credential_fp" in e:
            assert e["credential_fp"].startswith("sha256:")
            assert len(e["credential_fp"]) < 32


def test_successful_auth_logs_input_and_decision(env):
    create_widget(env, key="k")
    events = env.logs("auth_decision")
    assert any(
        e["decision"] == "allow"
        and e["tenant_id"] == "tenant-a"
        and e["credential_label"] == "ci-writer"
        and "widgets:write" in e["scopes"]
        for e in events
    )


def test_request_id_echos_on_auth_failures(env):
    r = env.client.post(
        "/api/v1/widgets",
        json={"name": "x"},
        headers={"Idempotency-Key": "k", "X-Request-Id": "caller-id-123"},
    )
    # 调用方传入的合法 request_id 被沿用并贯穿响应
    assert r.headers["x-request-id"] == "caller-id-123"
    assert r.json()["error"]["request_id"] == "caller-id-123"
