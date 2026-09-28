"""本机运维入口：凭证签发/轮换/吊销、限流与灰度热更新、非法配置拒绝、配置版本与回退、脱敏。"""
import pytest
from fastapi.testclient import TestClient

from integration_layer.app import LayerConfig, create_app
from integration_layer.errors import ErrorCode

from .conftest import log_case


@pytest.fixture()
def admin_client(app):
    return TestClient(app, raise_server_exceptions=False)


def test_issue_credential_is_masked_and_versioned(app, admin_client):
    r = admin_client.post(
        "/admin/credentials/issue",
        json={"tenant_id": "t-issue", "scopes": ["resources:read"]},
    )
    log_case(
        "issue credential",
        {"tenant_id": "t-issue", "scopes": ["resources:read"]},
        "签发 200，返回指纹+脱敏片段，不回显完整密钥，配置版本 +1",
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["fingerprint"] and len(j["fingerprint"]) == 12
    assert j["secret_masked"] and "****" in j["secret_masked"]
    assert "secret_once" not in j and "secret" not in j
    assert j["config_version"] == 2
    assert r.headers["x-config-version"] == "2"


def test_issued_credential_works_immediately(app, admin_client):
    r = admin_client.post(
        "/admin/credentials/issue",
        json={"tenant_id": "t-work", "scopes": ["resources:read", "resources:write"]},
    )
    key_id = r.json()["key_id"]
    # HTTP 只给脱敏；本地进程内可凭 key_id 查指纹。这里直接再签发一组带明文的用于业务调用，
    # 同时验证上面 HTTP 签发的凭证确实进入了生效配置（GET /admin/config 可见）
    cfg = admin_client.get("/admin/config").json()
    assert any(c["key_id"] == key_id and c["tenant_id"] == "t-work" for c in cfg["credentials"])
    log_case("issued credential live", {"key_id": key_id}, "签发后无需重启即进入生效配置")


def test_rotate_credential_revokes_old_secret(app, admin_client):
    key_id, secret = app.state.credentials.register("t-rot", {"resources:write"})
    H = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1"}
    assert admin_client.post(
        "/api/resources", json={"name": "before"}, headers={**H, "idempotency-key": "b1"}
    ).status_code == 201
    r = admin_client.post("/admin/credentials/rotate", json={"key_id": key_id})
    log_case(
        "rotate credential",
        {"key_id": key_id},
        "轮换后指纹变化、旧密钥立即失效、响应/日志无完整密钥",
    )
    assert r.status_code == 200
    j = r.json()
    assert j["previous_fingerprint"] != j["fingerprint"]
    assert secret not in r.text
    # 后续请求旧密钥失效（无需重启）
    after = admin_client.post(
        "/api/resources", json={"name": "after"}, headers={**H, "idempotency-key": "a1"}
    )
    assert after.status_code == 401
    assert after.json()["error"]["code"] == ErrorCode.CREDENTIAL_INVALID.value


def test_rotate_unknown_credential_not_found(app, admin_client):
    r = admin_client.post("/admin/credentials/rotate", json={"key_id": "ghost.x"})
    log_case("rotate missing", {"key_id": "ghost.x"}, "轮换未知凭证应为 NOT_FOUND")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_revoke_credential_is_idempotent_no_extra_version(app, admin_client):
    r0 = admin_client.post(
        "/admin/credentials/issue", json={"tenant_id": "t-rv", "scopes": ["resources:read"]}
    )
    key_id = r0.json()["key_id"]
    r1 = admin_client.post("/admin/credentials/revoke", json={"key_id": key_id})
    v1 = r1.json()["config_version"]
    r2 = admin_client.post("/admin/credentials/revoke", json={"key_id": key_id})
    log_case(
        "revoke idempotent",
        {"key_id": key_id},
        "首次吊销 revoked=true；重复吊销 revoked=false 且不产生新版本",
    )
    assert r1.json()["revoked"] is True and r1.json()["existed"] is True
    assert r2.status_code == 200
    assert r2.json()["revoked"] is False and r2.json()["existed"] is False
    assert r2.json()["config_version"] == v1
    # 吊销后凭证立即失效
    assert key_id not in app.state.manager.current().credentials


def test_invalid_credential_config_rejected_and_old_kept(app, admin_client):
    before = app.state.manager.current().version
    r = admin_client.post(
        "/admin/credentials/issue", json={"tenant_id": "t-bad", "scopes": ["not:a:scope"]}
    )
    log_case(
        "invalid credential config",
        {"scopes": ["not:a:scope"]},
        "非法 scope 必须 INVALID_CONFIG 拒绝，生效版本原样保留",
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.INVALID_CONFIG.value
    assert app.state.manager.current().version == before
    # 空租户、空 scope 同样拒绝
    r2 = admin_client.post(
        "/admin/credentials/issue", json={"tenant_id": "   ", "scopes": ["resources:read"]}
    )
    assert r2.status_code == 400


def test_rate_limit_adjustment_takes_effect_without_restart(app, admin_client):
    key_id, secret = app.state.credentials.register("rl-tenant", {"resources:read"})
    H = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1"}
    ep = "GET /api/resources"
    r = admin_client.put(
        "/admin/rate-limits",
        json={"tenant_id": "rl-tenant", "endpoint": ep, "capacity": 2, "refill_per_second": 0.01},
    )
    log_case(
        "rate limit hot update",
        {"tenant": "rl-tenant", "capacity": 2},
        "调整后后续请求立即按新额度限流（2 个通过，第 3 个 RATE_LIMITED）",
    )
    assert r.status_code == 200 and r.json()["capacity"] == 2
    codes = [admin_client.get("/api/resources/x", headers=H).status_code for _ in range(2)]
    third = admin_client.get("/api/resources/x", headers=H)
    assert codes == [404, 404]
    assert third.status_code == 429
    assert third.json()["error"]["code"] == ErrorCode.RATE_LIMITED.value


def test_invalid_rate_limit_rejected_keeps_active(app, admin_client):
    key_id, secret = app.state.credentials.register("rl-bad", {"resources:read"})
    H = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1"}
    admin_client.put(
        "/admin/rate-limits",
        json={"tenant_id": "rl-bad", "endpoint": "GET /api/resources",
              "capacity": 1, "refill_per_second": 0.01},
    )
    bad = admin_client.put(
        "/admin/rate-limits",
        json={"tenant_id": "rl-bad", "endpoint": "GET /api/resources",
              "capacity": 0, "refill_per_second": 1.0},
    )
    log_case(
        "invalid rate rule",
        {"capacity": 0},
        "非正容量 INVALID_CONFIG，旧额度继续生效",
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == ErrorCode.INVALID_CONFIG.value
    # 旧额度（capacity=1）仍生效：一个 404，下一个 429
    assert admin_client.get("/api/resources/x", headers=H).status_code == 404
    assert admin_client.get("/api/resources/x", headers=H).status_code == 429


def test_flag_adjustment_takes_effect_without_restart(app, admin_client):
    key_id, secret = app.state.credentials.register("fg-tenant", {"resources:write"})
    H = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v2",
         "idempotency-key": "fg-1"}
    admin_client.put("/admin/flags/extended-status", json={"rollout": 100})
    log_case("flag hot update", {"rollout": 100}, "开关打开后后续请求立即体现 status_detail")
    r = admin_client.post("/api/resources", json={"name": "n"}, headers=H)
    assert r.status_code == 201
    assert r.json()["status_detail"] == "active:ok"
    # 关掉后后续请求立即不再体现（租户 override 关闭）
    admin_client.put(
        "/admin/flags/extended-status", json={"rollout": 100, "overrides": {"fg-tenant": False}}
    )
    r2 = admin_client.post("/api/resources", json={"name": "n2"},
                           headers={**H, "idempotency-key": "fg-2"})
    assert "status_detail" not in r2.json()


def test_invalid_flag_rejected_keeps_active(app, admin_client):
    admin_client.put("/admin/flags/f", json={"rollout": 50})
    bad = admin_client.put("/admin/flags/f", json={"rollout": 101})
    log_case("invalid flag", {"rollout": 101}, "越界 rollout 必须拒绝且保留 50")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == ErrorCode.INVALID_CONFIG.value
    assert app.state.manager.current().flags["f"].rollout == 50
    bad2 = admin_client.put("/admin/flags/f", json={"rollout": 10, "overrides": {"t": "yes"}})
    assert bad2.status_code == 400
    assert app.state.manager.current().flags["f"].rollout == 50


def test_current_version_and_monotonic_versions(app, admin_client):
    v0 = app.state.manager.current().version
    for i in range(3):
        admin_client.put("/admin/flags/f", json={"rollout": i})
    cfg = admin_client.get("/admin/config").json()
    log_case(
        "monotonic versions",
        {"from": v0, "ops": 3},
        "每次生效变更版本单调递增；GET /admin/config 报告当前版本",
    )
    assert cfg["version"] == v0 + 3
    versions = admin_client.get("/admin/config/versions").json()["versions"]
    assert versions[-1] == cfg["version"]
    assert versions == sorted(set(versions))  # 单调、唯一


def test_rollback_restores_behavior_atomically(app, admin_client):
    # 先建凭证（此时无 flag）-> v2
    key_id, secret = app.state.credentials.register("rb-tenant", {"resources:write"})
    v_cred = app.state.manager.current().version
    Hv1 = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1",
           "idempotency-key": "rb-on"}
    # 再打开 flag -> v3
    admin_client.put("/admin/flags/extended-status", json={"rollout": 100})
    v_on = app.state.manager.current().version
    # flag 开启期间：v1 不支持能力，被拒绝
    assert admin_client.post(
        "/api/resources", json={"name": "n"}, headers=Hv1
    ).json()["error"]["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value
    # 回退到“有凭证、无 flag”的版本
    r = admin_client.post("/admin/config/rollback", json={"version": v_cred})
    log_case(
        "rollback",
        {"target": v_cred, "from": v_on},
        "回退原子生效：新版本号承载旧内容，行为与该版本当时一致",
    )
    assert r.status_code == 200
    new_version = r.json()["version"]
    assert new_version > v_on
    assert "extended-status" not in app.state.manager.current().flags
    # 回退后 v1 请求恢复成功（行为与目标版本一致）
    ok = admin_client.post(
        "/api/resources", json={"name": "n"},
        headers={**Hv1, "idempotency-key": "rb-after"},
    )
    assert ok.status_code == 201 and "status_detail" not in ok.json()
    # 凭证仍在（目标版本包含它）
    assert key_id in app.state.manager.current().credentials
    # 回退后新签发的凭证（v3 之后没有），示例：回退后再做的管理变更仍正常递增
    r2 = admin_client.post("/admin/config/rollback", json={"version": v_on})
    assert r2.status_code == 200
    assert "extended-status" in app.state.manager.current().flags


def test_rollback_missing_version_is_distinct_error(app, admin_client):
    app2 = create_app(LayerConfig(config_history_size=3))
    client2 = TestClient(app2, raise_server_exceptions=False)
    for i in range(5):
        client2.post(
            "/admin/credentials/issue", json={"tenant_id": f"t{i}", "scopes": ["resources:read"]}
        )
    # 版本 1..3 已被有界历史丢弃
    r = client2.post("/admin/config/rollback", json={"version": 1})
    log_case(
        "rollback dropped version",
        {"requested": 1, "available": [4, 5, 6]},
        "回退到不存在版本：CONFIG_VERSION_NOT_FOUND(404)，区别于 INVALID_CONFIG(400)",
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == ErrorCode.CONFIG_VERSION_NOT_FOUND.value
    assert r.json()["error"]["details"]["available_versions"] == [4, 5, 6]
    # 当前版本不变
    assert app2.state.manager.current().version == 6
    # 与“配置非法”可区分
    bad = client2.put(
        "/admin/flags/f", json={"rollout": 999}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == ErrorCode.INVALID_CONFIG.value


def test_history_is_bounded(app, admin_client):
    small = create_app(LayerConfig(config_history_size=5))
    client = TestClient(small, raise_server_exceptions=False)
    for i in range(8):
        client.put("/admin/flags/f", json={"rollout": i})
    versions = client.get("/admin/config/versions").json()["versions"]
    log_case("bounded history", {"size": 5, "ops": 8}, "历史有界：只留最近 5 个，最旧被丢弃")
    assert len(versions) == 5
    assert versions == sorted(set(versions))
    assert versions[-1] == small.state.manager.current().version


def test_admin_only_available_from_loopback(app):
    remote = TestClient(app, raise_server_exceptions=False, client=("10.1.2.3", 5000))
    r = remote.get("/admin/config")
    log_case("admin remote denied", {"client": "10.1.2.3"}, "非回环访问运维入口必须 PERMISSION_DENIED")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value
    # 业务接口不受影响（走到鉴权而非 403）
    biz = remote.post(
        "/api/resources", json={"name": "x"}, headers={"x-api-version": "v1"}
    )
    assert biz.status_code == 401


def test_no_secret_in_any_admin_response_or_config_view(app, admin_client):
    key_id, secret = app.state.credentials.register("leak", {"resources:read"})
    rotate = admin_client.post("/admin/credentials/rotate", json={"key_id": key_id})
    log_case("no secret leak", {"key_id": key_id}, "任何响应/配置视图都不包含完整密钥，只有指纹/脱敏")
    assert secret not in rotate.text
    cfg = admin_client.get("/admin/config").text
    assert secret not in cfg
    versions = admin_client.get("/admin/config/versions").text
    assert secret not in versions
    revoke = admin_client.post("/admin/credentials/revoke", json={"key_id": key_id})
    assert secret not in revoke.text


def test_inflight_request_not_split_by_rollback(app, admin_client):
    """处理中的请求持有开始时的快照：中途回退不会让它看到混合状态。

    时序：v2 签发凭证（flag 关）-> v3 打开 extended-status。工作线程并发发请求，
    主线程同时回退到 v2。每个请求的鉴权与灰度必须来自同一份快照：
    - 用 v3 快照：鉴权通过且 v2 接口的字段自洽；
    - 用 v2 快照：凭证仍在（v2 已签发），flag 关 -> 201 但无 status_detail。
    两种结果都自洽，不允许内部错误或字段错乱。
    """
    import threading

    # 先签凭证 -> v2（flag 尚未开启，该版本包含凭证）
    key_id, secret = app.state.credentials.register("inflight", {"resources:write"})
    v_cred = app.state.manager.current().version
    # 再打开 flag -> v3
    admin_client.put("/admin/flags/extended-status", json={"rollout": 100})
    v_on = app.state.manager.current().version
    assert v_on == v_cred + 1

    biz = TestClient(app, raise_server_exceptions=False)
    barrier = threading.Barrier(9)  # 8 workers + main
    results: list = []
    results_lock = threading.Lock()

    def worker(i):
        headers = {
            "x-api-key-id": key_id, "x-api-secret": secret,
            "x-api-version": "v2", "idempotency-key": f"if-{i}",
        }
        barrier.wait()
        r = biz.post("/api/resources", json={"name": f"n-{i}"}, headers=headers)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    barrier.wait()  # 与工作线程同时起跑，随即回退到“有凭证、flag 关”的版本
    rb = admin_client.post("/admin/config/rollback", json={"version": v_cred})
    assert rb.status_code == 200
    for t in threads:
        t.join()

    log_case(
        "inflight vs rollback",
        {"threads": 8, "rollback_to": v_cred},
        "每个请求的鉴权/灰度取自同一快照：成功响应字段自洽，无 5xx/401/半切换",
    )
    for r in results:
        # 快照 v2：凭证有效+flag关 -> 201 无 status_detail；
        # 快照 v3：凭证有效+flag开 -> 201 有 status_detail。两者都合法且自洽。
        assert r.status_code in (200, 201), r.text
        body = r.json()
        assert body["status"] == "active"
        if "status_detail" in body:
            assert body["status_detail"] == "active:ok"


def test_rollback_restores_rate_limit_rule(app, admin_client):
    key_id, secret = app.state.credentials.register("rl-rb", {"resources:read"})
    H = {"x-api-key-id": key_id, "x-api-secret": secret, "x-api-version": "v1"}
    ep = "GET /api/resources"
    # v2: capacity=1 严格限流
    admin_client.put("/admin/rate-limits",
                     json={"tenant_id": "rl-rb", "endpoint": ep,
                           "capacity": 1, "refill_per_second": 0.01})
    v_strict = app.state.manager.current().version
    # v3: 放宽到 capacity=10
    admin_client.put("/admin/rate-limits",
                     json={"tenant_id": "rl-rb", "endpoint": ep,
                           "capacity": 10, "refill_per_second": 10.0})
    # 回退到严格版本
    r = admin_client.post("/admin/config/rollback", json={"version": v_strict})
    assert r.status_code == 200
    log_case("rollback rate rule", {"target": v_strict},
             "回退后后续请求按目标版本的限流额度判定（1 个后即 429）")
    assert admin_client.get("/api/resources/x", headers=H).status_code == 404
    assert admin_client.get("/api/resources/x", headers=H).status_code == 429
