"""本轮修复回归：创建响应字段保留与重放一致、吊销空操作不推进配置版本、
删除后存活统计归零与重复删除一致。"""
import pytest

from integration_layer.app import LayerConfig, create_app
from integration_layer.errors import ErrorCode

from .conftest import log_case


@pytest.fixture()
def stack():
    app = create_app(LayerConfig(config_history_limit=4))
    from fastapi.testclient import TestClient

    return app, TestClient(app, raise_server_exceptions=False)


def _issue(app, c, tenant="t-bf", scopes=("resources:read", "resources:write"),
           secret="bf-secret-12345"):
    r = c.post(
        "/admin/credentials",
        json={"tenant_id": tenant, "scopes": list(scopes), "secret": secret},
    )
    assert r.status_code == 200, r.text
    return r.json()["credential"]["key_id"], secret


def _headers(key_id, secret, version):
    return {
        "x-api-key-id": key_id,
        "x-api-secret": secret,
        "x-api-version": version,
    }


# ---- 创建响应：name 字段保留 + 同键重放逐字一致 ----

@pytest.mark.parametrize("version", ["v1", "v2"])
def test_create_response_keeps_name_field(stack, version):
    app, c = stack
    key_id, secret = _issue(app, c)
    h = _headers(key_id, secret, version)
    r = c.post(
        "/api/resources",
        json={"name": "widget-42", "labels": {"k": "v"}} if version == "v2"
        else {"name": "widget-42"},
        headers={**h, "idempotency-key": "bf-c1"},
    )
    log_case(
        "create keeps name",
        {"version": version},
        "创建成功响应必须仍包含创建时提交的 name（迭代前老行为，v1/v2 均保留）",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "widget-42"
    # 老行为字段都在
    assert {"id", "name", "status", "created_at"} <= set(body)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_create_replay_returns_exact_same_body(stack, version):
    app, c = stack
    key_id, secret = _issue(app, c)
    h = _headers(key_id, secret, version)
    payload = {"name": "replay-me"}
    if version == "v2":
        payload["labels"] = {"a": "b"}
    r1 = c.post("/api/resources", json=payload, headers={**h, "idempotency-key": "bf-r1"})
    r2 = c.post("/api/resources", json=payload, headers={**h, "idempotency-key": "bf-r1"})
    b1, b2 = r1.json(), r2.json()
    b1.pop("request_id")
    b2.pop("request_id")
    log_case(
        "create replay identical incl name",
        {"version": version, "key": "bf-r1"},
        "同幂等键重放：状态码与全部响应字段（含 name）逐字一致，不产生第二个资源",
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert b1 == b2
    assert b2["name"] == "replay-me"
    # 副作用一次：重放没有再创建资源
    assert app.state.resources.count("t-bf") == 1


# ---- 吊销幂等：不存在的凭证吊销不推进配置版本 ----

def test_revoke_missing_credential_does_not_bump_version(stack):
    app, c = stack
    key_id, secret = _issue(app, c)
    v_issued = c.get("/admin/config/version").json()["current_version"]

    d1 = c.post(f"/admin/credentials/{key_id}/revoke")
    v_after_real = c.get("/admin/config/version").json()["current_version"]
    assert d1.status_code == 200 and d1.json()["existed"] is True
    assert v_after_real == v_issued + 1  # 真实吊销推进一次

    d2 = c.post(f"/admin/credentials/{key_id}/revoke")
    d3 = c.post("/admin/credentials/{key_id}/revoke")
    d4 = c.post("/admin/credentials/never-issued.0000/revoke")
    v_after_noop = c.get("/admin/config/version").json()["current_version"]
    log_case(
        "revoke no-op no version bump",
        {"real_revoke": 1, "empty_revokes": 3,
         "version_before_noop": v_after_real, "version_after_noop": v_after_noop},
        "吊销已吊销/从未签发的凭证：无实际变更，配置版本不再前进；响应仍稳定可解释",
    )
    for d in (d2, d3, d4):
        assert d.status_code == 200
        assert d.json()["revoked"] is True
        assert d.json()["existed"] is False
    assert v_after_noop == v_after_real
    # 空操作的响应里带的 config_version 同样停在真实版本
    assert d2.json()["config_version"] == v_after_real
    # 凭证确实处于已吊销状态（空吊销没有把它恢复）
    h = _headers(key_id, secret, "v1")
    assert c.get("/api/resources/x", headers=h).status_code == 401


def test_repeated_empty_revokes_do_not_evict_real_versions(stack):
    """有界历史（limit=4）：空吊销不占历史槽位，真实生效过的版本仍可回退。"""
    _app, c = stack
    # 制造 3 次真实变更：v2, v3, v4
    c.put("/admin/flags/f0", json={"rollout": 0})
    c.put("/admin/flags/f1", json={"rollout": 0})
    c.put("/admin/flags/f2", json={"rollout": 0})
    hist = c.get("/admin/config/version").json()["history_versions"]
    assert hist == [1, 2, 3, 4]  # 恰好到界

    # 对不存在的 key 反复吊销：不得顶掉版本 1
    for i in range(10):
        r = c.post("/admin/credentials/ghost.abcd/revoke")
        assert r.status_code == 200
    hist_after = c.get("/admin/config/version").json()["history_versions"]
    log_case(
        "empty revokes preserve history",
        {"history_before": hist, "history_after": hist_after, "empty_revokes": 10},
        "空吊销不产生新版本、不占用有界历史槽位，旧的真实版本不被顶掉",
    )
    assert hist_after == [1, 2, 3, 4]
    # 最旧真实版本仍可回退（修复前会被空操作版本顶掉而 404）
    rb = c.post("/admin/config/rollback", json={"version": 1})
    assert rb.status_code == 200, rb.text


def test_revoke_real_then_config_history_boundary_still_works(stack):
    """真实吊销占一个版本；随后空吊销不继续挤占，回退目标仍可用。"""
    app, c = stack
    key_id, _ = _issue(app, c, tenant="t-edge")  # v2
    c.put("/admin/flags/f0", json={"rollout": 0})  # v3
    c.put("/admin/flags/f1", json={"rollout": 0})  # v4 历史=[1,2,3,4]
    c.post(f"/admin/credentials/{key_id}/revoke")  # v5 历史=[2,3,4,5]，1 被正常淘汰
    for _ in range(5):
        c.post("/admin/credentials/{key_id}/revoke")
    # 版本 2（凭证仍有效）没被空吊销顶掉，可回退恢复凭证
    rb = c.post("/admin/config/rollback", json={"version": 2})
    log_case(
        "real revoke then empty revokes rollback",
        {"rollback_target": 2},
        "真实变更照常推进与淘汰；空吊销不额外推进，真实版本 2 保留并可回退",
    )
    assert rb.status_code == 200


# ---- 删除：存活统计归零、重复删除一致、已删查不到 ----

def test_deleted_resource_not_counted_as_alive(stack):
    app, c = stack
    key_id, secret = _issue(app, c)
    h = _headers(key_id, secret, "v2")
    ids = []
    for i in range(3):
        r = c.post("/api/resources", json={"name": f"r{i}"},
                   headers={**h, "idempotency-key": f"bf-dc{i}"})
        assert r.status_code == 201
        ids.append(r.json()["id"])
    assert app.state.resources.count("t-bf") == 3

    # 删掉其中两个：存活数随之下降，墓碑不计入
    c.delete(f"/api/resources/{ids[0]}", headers={**h, "idempotency-key": "bf-dd0"})
    c.delete(f"/api/resources/{ids[1]}", headers={**h, "idempotency-key": "bf-dd1"})
    assert app.state.resources.count("t-bf") == 1
    # 重复删除（新幂等键）不改变存活统计
    c.delete(f"/api/resources/{ids[0]}", headers={**h, "idempotency-key": "bf-dd0b"})
    c.delete(f"/api/resources/{ids[1]}", headers={**h, "idempotency-key": "bf-dd1b"})

    c.delete(f"/api/resources/{ids[2]}", headers={**h, "idempotency-key": "bf-dd2"})
    log_case(
        "delete counts only alive",
        {"created": 3, "deleted": 3, "alive_after": 0},
        "删除后墓碑不计入存活数量；重复删除不产生残留计数，存活数归零",
    )
    assert app.state.resources.count("t-bf") == 0
    # 其他租户不受影响
    assert app.state.resources.count("other-tenant") == 0


def test_delete_repeated_consistent_and_get_404(stack):
    app, c = stack
    key_id, secret = _issue(app, c)
    h = _headers(key_id, secret, "v2")
    rid = c.post("/api/resources", json={"name": "x"},
                 headers={**h, "idempotency-key": "bf-g1"}).json()["id"]

    d1 = c.delete(f"/api/resources/{rid}", headers={**h, "idempotency-key": "bf-gd1"})
    # 同键重放
    d2 = c.delete(f"/api/resources/{rid}", headers={**h, "idempotency-key": "bf-gd1"})
    # 新键重复删
    d3 = c.delete(f"/api/resources/{rid}", headers={**h, "idempotency-key": "bf-gd2"})
    g = c.get(f"/api/resources/{rid}", headers=h)
    b1, b2, b3 = d1.json(), d2.json(), d3.json()
    for b in (b1, b2, b3):
        b.pop("request_id")
    log_case(
        "delete repeat consistency",
        {"same_key_replay": True, "new_key_repeat": True},
        "重复删除（同键/新键）响应一致 deleted=true；已删资源 GET 稳定 404",
    )
    assert d1.status_code == d2.status_code == d3.status_code == 200
    assert b1 == b2 == b3
    assert b1["deleted"] is True
    assert g.status_code == 404
    assert g.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_delete_never_existed_is_stable(stack):
    app, c = stack
    key_id, secret = _issue(app, c)
    h = _headers(key_id, secret, "v2")
    d1 = c.delete("/api/resources/ghost-id", headers={**h, "idempotency-key": "bf-n1"})
    d2 = c.delete("/api/resources/ghost-id", headers={**h, "idempotency-key": "bf-n1"})
    d3 = c.delete("/api/resources/ghost-id", headers={**h, "idempotency-key": "bf-n2"})
    log_case(
        "delete never-existed",
        {"id": "ghost-id"},
        "删除从未存在的资源：稳定 200、deleted=true，多次结果一致，不影响统计",
    )
    assert all(d.status_code == 200 for d in (d1, d2, d3))
    b1, b2, b3 = d1.json(), d2.json(), d3.json()
    for b in (b1, b2, b3):
        b.pop("request_id")
    assert b1 == b2 == b3
    assert app.state.resources.count("t-bf") == 0
