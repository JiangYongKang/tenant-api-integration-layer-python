"""统一错误契约测试：可区分、无泄露、request_id 贯穿。"""

from __future__ import annotations

from tests.conftest import assert_error


def test_all_error_shapes_share_contract(env):
    token = env.raw_token("tenant-a", "ci-writer")
    # 认证失败
    r1 = env.client.post("/api/v1/widgets", json={"name": "x"},
                         headers={"Idempotency-Key": "k"})
    # 校验失败
    r2 = env.client.post("/api/v1/widgets", json={},
                         headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k2"})
    # 限流
    tb = env.raw_token("tenant-b", "writer")
    for i in range(4):
        r3 = env.client.post("/api/v1/widgets", json={"name": "n"},
                             headers={"Authorization": f"Bearer {tb}", "Idempotency-Key": f"r{i}"})
    for r, status, code in [
        (r1, 401, "credential_missing"),
        (r2, 422, "validation_failed"),
        (r3, 429, "rate_limited"),
    ]:
        err = assert_error(r, status, code)
        assert set(err.keys()) >= {"code", "message", "request_id"}


def test_error_codes_are_distinct(env):
    # 四种鉴权类失败码互不相同
    token = env.raw_token("tenant-b", "writer")
    expired = env.raw_token("tenant-b", "expired-one")
    codes = []
    codes.append(env.client.post("/api/v1/widgets", json={"name": "x"},
                                 headers={"Idempotency-Key": "a"}).json()["error"]["code"])
    codes.append(env.client.post("/api/v1/widgets", json={"name": "x"},
                                 headers={"Authorization": "Bearer bad.token.x",
                                          "Idempotency-Key": "b"}).json()["error"]["code"])
    codes.append(env.client.post("/api/v1/widgets", json={"name": "x"},
                                 headers={"Authorization": f"Bearer {expired}",
                                          "Idempotency-Key": "c"}).json()["error"]["code"])
    codes.append(env.client.post("/api/v1/widgets", json={"name": "x"},
                                 headers=env.auth("tenant-a", "ci-reader") | {"Idempotency-Key": "d"},
                                 ).json()["error"]["code"])
    assert codes == ["credential_missing", "credential_invalid",
                     "credential_expired", "permission_denied"]


def test_request_id_runs_through_log_and_response(env):
    r = env.client.post("/api/v1/widgets", json={"name": "x"},
                        headers={**env.auth("tenant-a", "ci-writer"),
                                 "Idempotency-Key": "rid-1",
                                 "X-Request-Id": "trace-abc-001"})
    assert r.headers["x-request-id"] == "trace-abc-001"
    rid_in_body = r.json().get("error", {}).get("request_id")
    # 成功响应无 error 体；通过 header 与日志验证
    logged = [e for e in env.logs() if "trace-abc-001" in repr(e)]
    assert logged, "request_id 应出现在结构化日志中"
    # 再发一个失败请求验证 body 中的贯穿
    fail = env.client.post("/api/v1/widgets", json={"name": "x"},
                           headers={"Idempotency-Key": "rid-2",
                                    "X-Request-Id": "trace-abc-002"})
    assert fail.json()["error"]["request_id"] == "trace-abc-002"


def test_no_stack_or_internal_path_in_error_response(env, monkeypatch):
    """制造内部异常时，响应不得包含堆栈、文件路径或异常原文。"""
    # 让存储创建抛异常，模拟部分失败
    def boom(_item):
        raise RuntimeError("secret internal detail at /opt/secret/db")

    monkeypatch.setattr(env.deps.resources, "create", boom)
    r = env.client.post("/api/v1/widgets", json={"name": "x"},
                        headers={**env.auth("tenant-a", "ci-writer"), "Idempotency-Key": "boom"})
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "internal_error"
    assert "secret internal detail" not in r.text
    assert "/opt/secret" not in r.text
    assert "Traceback" not in r.text
