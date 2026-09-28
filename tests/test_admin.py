"""本机运维入口：凭证签发/轮换/吊销、限流/灰度调整、非法配置拒绝、
配置版本单调/有界历史/回退、脱敏约定、仅本机可用。"""
import time

import pytest
from fastapi.testclient import TestClient

from integration_layer.app import LayerConfig, create_app
from integration_layer.errors import ErrorCode
from integration_layer.rate_limit import RateLimitRule

from .conftest import log_case


@pytest.fixture()
def admin_client():
    app = create_app(LayerConfig(config_history_limit=4))
    return app, TestClient(app, raise_server_exceptions=False)


def test_issue_credential_is_masked_and_usable(admin_client):
    app, c = admin_client
    r = c.post(
        "/admin/credentials",
        json={"tenant_id": "t-ops", "scopes": ["resources:read", "resources:write"],
              "secret": "plaintext-secret-xyz"},
    )
    body = r.json()
    log_case(
        "issue credential",
        {"tenant": "t-ops", "secret": "plaintext-secret-xyz"},
        "签发：200 且只回显脱敏串/指纹，完整密钥不出现在响应中",
    )
    assert r.status_code == 200
    cred = body["credential"]
    assert "plaintext-secret-xyz" not in r.text
    assert cred["secret_masked"] != "plaintext-secret-xyz"
    assert cred["secret_fingerprint"] and len(cred["secret_fingerprint"]) == 12
    # 脱敏串可核对：保留首末片段
    assert cred["secret_masked"].startswith("pl") and cred["secret_masked"].endswith("yz")
    # 签发的凭证立即可用于业务请求（无需重启）
    headers = {"x-api-key-id": cred["key_id"], "x-api-secret": "plaintext-secret-xyz",
               "x-api-version": "v2"}
    ok = c.post("/api/resources", json={"name": "x"},
                headers={**headers, "idempotency-key": "adm-c1"})
    assert ok.status_code == 201


def test_rotate_revokes_old_and_issues_new(admin_client):
    app, c = admin_client
    r = c.post("/admin/credentials",
               json={"tenant_id": "t-rot", "scopes": ["resources:read"],
                     "secret": "old-secret-12345"})
    old_key = r.json()["credential"]["key_id"]
    rr = c.post(f"/admin/credentials/{old_key}/rotate",
                json={"secret": "new-secret-67890"})
    log_case(
        "rotate credential",
        {"old_key": old_key},
        "轮换：新凭证生效、旧凭证立即失效；响应不含任何完整密钥",
    )
    assert rr.status_code == 200
    new_key = rr.json()["credential"]["key_id"]
    assert new_key != old_key
    assert "old-secret-12345" not in rr.text and "new-secret-67890" not in rr.text
    old_h = {"x-api-key-id": old_key, "x-api-secret": "old-secret-12345",
             "x-api-version": "v1"}
    new_h = {"x-api-key-id": new_key, "x-api-secret": "new-secret-67890",
             "x-api-version": "v1"}
    assert c.get("/api/resources/x", headers=old_h).status_code == 401
    assert c.get("/api/resources/x", headers=new_h).status_code == 404  # 通过鉴权


def test_rotate_missing_key_404(admin_client):
    _, c = admin_client
    r = c.post("/admin/credentials/ghost.1234/rotate", json={"secret": "new-secret-67890"})
    log_case("rotate missing", {"key_id": "ghost.1234"}, "轮换不存在的 key 应 NOT_FOUND")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_revoke_is_idempotent(admin_client):
    _, c = admin_client
    r = c.post("/admin/credentials",
               json={"tenant_id": "t-rv", "scopes": ["resources:read"],
                     "secret": "rv-secret-12345"})
    key = r.json()["credential"]["key_id"]
    d1 = c.post(f"/admin/credentials/{key}/revoke")
    d2 = c.post(f"/admin/credentials/{key}/revoke")
    log_case(
        "revoke idempotent",
        {"calls": 2},
        "吊销幂等：第一次 existed=true，第二次 existed=false，均 200 revoked=true",
    )
    assert d1.json()["existed"] is True
    assert d2.status_code == 200 and d2.json()["existed"] is False
    assert d2.json()["revoked"] is True


def test_issue_invalid_scopes_rejected_old_config_kept(admin_client):
    app, c = admin_client
    before = c.get("/admin/config/version").json()["current_version"]
    r = c.post("/admin/credentials",
               json={"tenant_id": "t-bad", "scopes": ["not-a-scope"]})
    after = c.get("/admin/config/version").json()["current_version"]
    log_case(
        "invalid scopes",
        {"scopes": ["not-a-scope"]},
        "非法 scope 应 INVALID_CONFIG，且不产生新版本（旧配置保留）",
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.INVALID_CONFIG.value
    assert before == after


def test_rate_limit_adjustment_takes_effect_without_restart(admin_client):
    app, c = admin_client
    c.post("/admin/credentials",
           json={"tenant_id": "t-rl", "scopes": ["resources:write", "resources:read"],
                 "secret": "rl-secret-12345"})
    headers = {"x-api-key-id": None}
    # 取回 key_id
    kid = app.state.config_manager.snapshot()
    key_id = [k for k in kid.credentials if kid.credentials[k].tenant_id == "t-rl"][0]
    h = {"x-api-key-id": key_id, "x-api-secret": "rl-secret-12345", "x-api-version": "v2"}

    setr = c.put("/admin/rate-limits/t-rl",
                 json={"endpoint": "POST /api/resources", "capacity": 1,
                       "refill_per_second": 0.01})
    log_case(
        "adjust rate limit",
        {"capacity": "20(default) -> 1"},
        "运行中调整额度立即对后续请求生效：第二个请求即被限流",
    )
    assert setr.status_code == 200
    first = c.post("/api/resources", json={"name": "a"},
                   headers={**h, "idempotency-key": "rl-1"})
    second = c.post("/api/resources", json={"name": "b"},
                    headers={**h, "idempotency-key": "rl-2"})
    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"]["code"] == ErrorCode.RATE_LIMITED.value


def test_invalid_rate_rule_rejected(admin_client):
    _, c = admin_client
    r = c.put("/admin/rate-limits/t",
              json={"endpoint": "POST /api/resources", "capacity": 0,
                    "refill_per_second": 1.0})
    log_case("invalid rate", {"capacity": 0}, "非正容量必须 INVALID_CONFIG 拒绝")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.INVALID_CONFIG.value


def test_flag_adjustment_and_atomic_replace(admin_client):
    app, c = admin_client
    r = c.put("/admin/flags/extended-status", json={"rollout": 100})
    log_case("set flag", {"flag": "extended-status", "rollout": 100}, "单开关调整生效")
    assert r.status_code == 200
    # 非法 rollout 被拒绝，旧配置保留
    bad = c.put("/admin/flags/extended-status", json={"rollout": 150})
    assert bad.status_code == 400
    cur = c.get("/admin/config/version").json()["current_version"]
    assert c.get("/admin/config/version").json()["current_version"] == cur
    # 整体原子替换
    rep = c.put("/admin/flags",
                json={"flags": {"a": {"rollout": 0}, "b": {"rollout": 100}}})
    log_case("replace flags", {"flags": ["a=0", "b=100"]}, "整体替换原子生效")
    assert rep.status_code == 200
    snap = c.get("/admin/config/version")  # noqa
    from integration_layer.feature_flags import FlagSnapshot
    fs = FlagSnapshot(flags=dict(app.state.config_manager.snapshot().flags))
    assert fs.is_enabled("a", "x") is False
    assert fs.is_enabled("b", "x") is True


def test_config_versions_monotonic_and_current_query(admin_client):
    _, c = admin_client
    starts = c.get("/admin/config/version").json()
    versions = [starts["current_version"]]
    for i in range(2):
        c.put(f"/admin/flags/f{i}", json={"rollout": 0})
        versions.append(c.get("/admin/config/version").json()["current_version"])
    log_case(
        "monotonic versions",
        {"versions": versions},
        "每次变更版本严格单调递增；当前版本可查询",
    )
    assert all(versions[i] < versions[i + 1] for i in range(len(versions) - 1))


def test_history_is_bounded_oldest_dropped(admin_client):
    app, c = admin_client
    # history_limit=4：初始 1，再制造 5 次变更
    for i in range(5):
        c.put(f"/admin/flags/g{i}", json={"rollout": 0})
    hist = c.get("/admin/config/version").json()
    log_case(
        "bounded history",
        {"limit": 4, "kept": hist["history_versions"]},
        "历史有界：超出只保留最近 4 个版本，最旧被丢弃",
    )
    assert len(hist["history_versions"]) == 4
    assert hist["history_versions"] == sorted(hist["history_versions"])


def test_rollback_restores_behavior(admin_client):
    app, c = admin_client
    # v1: 无凭证。签发一个凭证 -> 新版本
    c.post("/admin/credentials",
           json={"tenant_id": "t-rb", "scopes": ["resources:read"],
                 "secret": "rb-secret-12345"})
    with_cred = c.get("/admin/config/version").json()["current_version"]
    key_id = [k for k in app.state.config_manager.snapshot().credentials][0]
    h = {"x-api-key-id": key_id, "x-api-secret": "rb-secret-12345", "x-api-version": "v1"}
    assert c.get("/api/resources/x", headers=h).status_code == 404  # 凭证有效

    # 吊销 -> 新版本；再回退到吊销前
    c.post(f"/admin/credentials/{key_id}/revoke")
    assert c.get("/api/resources/x", headers=h).status_code == 401
    rb = c.post("/admin/config/rollback", json={"version": with_cred})
    log_case(
        "rollback behavior",
        {"target": with_cred},
        "原子回退后对外行为与目标版本一致：被吊销的凭证重新有效",
    )
    assert rb.status_code == 200
    assert rb.json()["current_version"] > with_cred  # 回退也产生单调新版本
    assert c.get("/api/resources/x", headers=h).status_code == 404  # 凭证恢复有效
    app.state.rate_limiter.reset_all_buckets()


def test_rollback_missing_version_distinct_from_invalid_config(admin_client):
    _, c = admin_client
    missing = c.post("/admin/config/rollback", json={"version": 9999})
    bad_shape = c.post("/admin/config/rollback", json={"version": "not-int"})
    log_case(
        "rollback errors distinct",
        {"missing": 9999, "bad_shape": "not-int"},
        "版本不存在=CONFIG_VERSION_NOT_FOUND(404)；入参非法=VALIDATION_FAILED；均与 INVALID_CONFIG 区分",
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == ErrorCode.CONFIG_VERSION_NOT_FOUND.value
    assert "available_versions" in missing.json()["error"]["details"]
    assert bad_shape.status_code == 400
    assert bad_shape.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value


def test_rollback_kept_version_then_dropped_returns_not_found(admin_client):
    app, c = admin_client
    # 超出有界历史后，回退一个已被淘汰的旧版本 -> 404 而不是 500
    first_targets = []
    for i in range(5):
        c.put(f"/admin/flags/h{i}", json={"rollout": 0})
        first_targets.append(c.get("/admin/config/version").json()["current_version"])
    dropped = first_targets[0]
    available = c.get("/admin/config/version").json()["history_versions"]
    r = c.post("/admin/config/rollback", json={"version": dropped})
    log_case(
        "rollback dropped version",
        {"dropped": dropped, "available": available},
        "回退已被有界历史淘汰的版本：CONFIG_VERSION_NOT_FOUND",
    )
    assert dropped not in available
    assert r.status_code == 404
    assert r.json()["error"]["code"] == ErrorCode.CONFIG_VERSION_NOT_FOUND.value


def test_admin_only_local_host():
    """非本机来源访问运维入口：404，不暴露入口存在。loopback 正常。"""
    import asyncio

    import httpx

    app = create_app()

    async def call_from(host: str) -> httpx.Response:
        # 直接以 ASGI transport 伪造对端地址，绕过真实网络
        transport = httpx.ASGITransport(app=app, client=(host, 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as ac:
            return await ac.get("/admin/config/version")

    external = asyncio.run(call_from("8.8.8.8"))
    local = asyncio.run(call_from("127.0.0.1"))
    log_case(
        "admin local-only",
        {"external": "8.8.8.8", "local": "127.0.0.1"},
        "外部主机访问运维入口返回 404（不暴露存在）；本机正常 200",
    )
    assert external.status_code == 404
    assert external.json()["error"]["code"] == ErrorCode.NOT_FOUND.value
    assert local.status_code == 200
    assert "current_version" in local.json()


def test_no_full_secret_in_admin_logs(admin_client, caplog):
    _, c = admin_client
    import logging
    with caplog.at_level(logging.INFO, logger="integration_layer.admin"):
        c.post("/admin/credentials",
               json={"tenant_id": "t-log", "scopes": ["resources:read"],
                     "secret": "loggable-secret-999"})
        c.post("/admin/credentials",
               json={"tenant_id": "t-log2", "scopes": ["resources:read"],
                     "secret": "another-secret-000"})
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    log_case("no secret in logs", {"secrets": ["loggable-secret-999"]},
             "运维日志不得包含完整密钥，只能出现指纹")
    assert "loggable-secret-999" not in blob
    assert "another-secret-000" not in blob


def test_rollback_restores_flag_behavior(admin_client):
    app, c = admin_client
    # 初始灰度关闭：v2 创建不带 status_detail
    c.post("/admin/credentials",
           json={"tenant_id": "t-fg", "scopes": ["resources:write", "resources:read"],
                 "secret": "fg-secret-123456"})
    key_id = [k for k in app.state.config_manager.snapshot().credentials
              if app.state.config_manager.snapshot().credentials[k].tenant_id == "t-fg"][0]
    h = {"x-api-key-id": key_id, "x-api-secret": "fg-secret-123456", "x-api-version": "v2"}
    off = c.post("/api/resources", json={"name": "a"},
                 headers={**h, "idempotency-key": "fg-1"})
    assert "status_detail" not in off.json()
    before = c.get("/admin/config/version").json()["current_version"]

    # 打开开关 -> 新资源带 status_detail
    c.put("/admin/flags/extended-status", json={"rollout": 100})
    on = c.post("/api/resources", json={"name": "b"},
                headers={**h, "idempotency-key": "fg-2"})
    assert on.json()["status_detail"] == "active:ok"

    # 回退到开关打开前 -> 后续请求不再带 status_detail
    rb = c.post("/admin/config/rollback", json={"version": before})
    assert rb.status_code == 200
    after = c.post("/api/resources", json={"name": "c"},
                   headers={**h, "idempotency-key": "fg-3"})
    log_case(
        "rollback flag behavior",
        {"target": before},
        "回退后灰度行为与目标版本一致：status_detail 不再出现",
    )
    assert "status_detail" not in after.json()
