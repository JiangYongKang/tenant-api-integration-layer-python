"""本轮 bugfix 回归：创建响应字段（v1/v2）、吊销空操作不推进配置版本、
删除后存活计数归零与重复删除一致。"""
import pytest
from fastapi.testclient import TestClient

from integration_layer.app import LayerConfig, create_app
from integration_layer.config_manager import ConfigManager, ConfigSnapshot

from .conftest import log_case

# ---- 创建响应必须回显资源名（迭代前老行为） ----

def test_v1_create_response_contains_name(client, tenant):
    _, headers = tenant
    r = client.post(
        "/api/resources",
        json={"name": "keep-name-v1"},
        headers={**headers, "idempotency-key": "bf-c1"},
    )
    log_case(
        "v1 create keeps name",
        {"name": "keep-name-v1"},
        "v1 创建成功响应必须仍含资源名（老行为不回归）",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "keep-name-v1"
    # v1 仍不含生命周期字段（行为冻结，只找回丢失的 name）
    assert "version" not in body and "updated_at" not in body and "deleted" not in body


def test_v2_create_response_contains_name_and_labels(client, v2_tenant):
    r = client.post(
        "/api/resources",
        json={"name": "keep-name-v2", "labels": {"env": "test"}},
        headers={**v2_tenant, "idempotency-key": "bf-c2"},
    )
    log_case(
        "v2 create keeps name",
        {"name": "keep-name-v2"},
        "v2 创建成功响应必须含资源名与 labels",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "keep-name-v2"
    assert body["labels"] == {"env": "test"}


def test_create_replay_returns_exactly_first_response(client, tenant):
    _, headers = tenant
    r1 = client.post(
        "/api/resources", json={"name": "replay-name"},
        headers={**headers, "idempotency-key": "bf-c3"},
    )
    r2 = client.post(
        "/api/resources", json={"name": "replay-name"},
        headers={**headers, "idempotency-key": "bf-c3"},
    )
    b1, b2 = r1.json(), r2.json()
    b1.pop("request_id"); b2.pop("request_id")
    log_case(
        "create replay keeps name",
        {"key": "bf-c3"},
        "同幂等键重放：响应（含 name）与第一次完全一致，不丢字段",
    )
    assert r1.status_code == 201 and r2.status_code == 200
    assert b1 == b2
    assert b2["name"] == "replay-name"


# ---- 吊销空操作不得推进配置版本、不得挤占有界历史 ----


@pytest.fixture()
def admin_client():
    app = create_app(LayerConfig(config_history_limit=4))
    return app, TestClient(app, raise_server_exceptions=False)


def test_revoke_nonexistent_does_not_bump_version(admin_client):
    _, c = admin_client
    before = c.get("/admin/config/version").json()
    # 从未签发过的 key：反复吊销
    responses = [
        c.post("/admin/credentials/never-issued/revoke") for _ in range(5)
    ]
    after = c.get("/admin/config/version").json()
    log_case(
        "revoke ghost no version bump",
        {"calls": 5, "key": "never-issued"},
        "吊销从未签发的凭证：无实际变更，配置版本与历史一次都不前进；响应稳定",
    )
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["revoked"] is True for r in responses)
    assert all(r.json()["existed"] is False for r in responses)
    assert after == before  # current_version 与 history_versions 均不变


def test_revoke_already_revoked_does_not_bump_version(admin_client):
    _, c = admin_client
    issued = c.post(
        "/admin/credentials",
        json={"tenant_id": "t-rv2", "scopes": ["resources:read"],
              "secret": "rv2-secret-12345"},
    )
    key = issued.json()["credential"]["key_id"]
    first = c.post(f"/admin/credentials/{key}/revoke")
    v_after_real = c.get("/admin/config/version").json()
    assert first.json()["existed"] is True
    # 已吊销后再反复吊销：版本冻结
    more = [c.post(f"/admin/credentials/{key}/revoke") for _ in range(5)]
    v_final = c.get("/admin/config/version").json()
    log_case(
        "repeat revoke no version bump",
        {"key": key, "repeat_calls": 5},
        "对已吊销凭证反复吊销：existed=false 且版本/历史不再变化",
    )
    assert all(r.status_code == 200 and r.json()["existed"] is False for r in more)
    assert v_final == v_after_real


def test_empty_revokes_do_not_evict_real_history(admin_client):
    """有界历史（limit=4）里真实版本不能被空吊销顶掉，回退始终可用。"""
    _app, c = admin_client
    # 制造若干真实变更版本，使历史接近上界
    versions = []
    for i in range(3):
        c.put(f"/admin/flags/marker-{i}", json={"rollout": 0})
        versions.append(c.get("/admin/config/version").json()["current_version"])
    target = versions[0]
    # 大量空吊销
    for i in range(20):
        c.post(f"/admin/credentials/ghost-{i}/revoke")
    hist = c.get("/admin/config/version").json()["history_versions"]
    log_case(
        "empty revoke preserves history",
        {"target": target, "history": hist, "empty_calls": 20},
        "20 次空吊销不推进版本、不挤占有界历史；真实版本仍可回退",
    )
    assert target in hist
    rb = c.post("/admin/config/rollback", json={"version": target})
    assert rb.status_code == 200, rb.text


def test_revoke_credential_unit_noop_skips_commit():
    """单元层：空吊销返回 False 且 current_version / 历史不变。"""
    mgr = ConfigManager(ConfigSnapshot(), history_limit=3)
    key_id, _ = mgr.issue_credential(
        "t-unit", {"resources:read"}, secret="unit-secret-123"
    )
    assert mgr.revoke_credential(key_id) is True
    v = mgr.current_version
    hist = mgr.history_versions()
    assert mgr.revoke_credential(key_id) is False
    assert mgr.revoke_credential("ghost") is False
    log_case(
        "unit revoke idempotent",
        {"real_revoke_version": v},
        "ConfigManager 层：真实吊销推进一次；之后空吊销完全不动版本与历史",
    )
    assert mgr.current_version == v
    assert mgr.history_versions() == hist


# ---- 删除干净：存活计数归零、重复删除一致、已删查不到 ----

def test_count_zero_after_delete_and_no_tombstone_residue(app, client, v2_tenant):
    r1 = client.post("/api/resources", json={"name": "a"},
                     headers={**v2_tenant, "idempotency-key": "bf-d1"})
    r2 = client.post("/api/resources", json={"name": "b"},
                     headers={**v2_tenant, "idempotency-key": "bf-d2"})
    assert app.state.resources.count("tenant-life") == 2
    client.delete(f"/api/resources/{r1.json()['id']}",
                  headers={**v2_tenant, "idempotency-key": "bf-dx1"})
    assert app.state.resources.count("tenant-life") == 1
    client.delete(f"/api/resources/{r2.json()['id']}",
                  headers={**v2_tenant, "idempotency-key": "bf-dx2"})
    log_case(
        "count zero after delete",
        {"created": 2, "deleted": 2},
        "删除后墓碑不计入存活数：两个资源删完存活数必须归零，无状态残留",
    )
    assert app.state.resources.count("tenant-life") == 0


def test_repeated_delete_consistent_and_get_404(app, client, v2_tenant):
    r = client.post("/api/resources", json={"name": "rd"},
                    headers={**v2_tenant, "idempotency-key": "bf-rd1"})
    rid = r.json()["id"]
    d1 = client.delete(f"/api/resources/{rid}",
                       headers={**v2_tenant, "idempotency-key": "bf-rdd1"})
    # 同键重放 + 新键重复删除 + 再新键，三者结果一致
    d2 = client.delete(f"/api/resources/{rid}",
                       headers={**v2_tenant, "idempotency-key": "bf-rdd1"})
    d3 = client.delete(f"/api/resources/{rid}",
                       headers={**v2_tenant, "idempotency-key": "bf-rdd2"})
    d4 = client.delete(f"/api/resources/{rid}",
                       headers={**v2_tenant, "idempotency-key": "bf-rdd3"})
    bodies = [d.json() for d in (d1, d2, d3, d4)]
    for b in bodies:
        b.pop("request_id")
    log_case(
        "repeated delete consistent",
        {"calls": 4, "same_key": 1, "new_keys": 2},
        "重复删除（同键/新键）全部 200 且响应一致；存活数始终 0；GET 恒 404",
    )
    assert [d.status_code for d in (d1, d2, d3, d4)] == [200, 200, 200, 200]
    assert bodies[0] == bodies[1] == bodies[2] == bodies[3]
    assert all(b["deleted"] is True for b in bodies)
    assert app.state.resources.count("tenant-life") == 0
    assert client.get(f"/api/resources/{rid}", headers=v2_tenant).status_code == 404
    # 已删资源的 id 不能被重建为活资源后串状态：新建独立资源计数仍准确
    r_new = client.post("/api/resources", json={"name": "rd2"},
                        headers={**v2_tenant, "idempotency-key": "bf-rd2"})
    assert r_new.status_code == 201
    assert app.state.resources.count("tenant-life") == 1
    assert client.get(f"/api/resources/{rid}", headers=v2_tenant).status_code == 404


def test_delete_never_existed_is_stable_and_count_zero(app, client, v2_tenant):
    # 对从未存在的 id 删除（新键）：200 合成墓碑，计数不受影响
    d1 = client.delete("/api/resources/ghost-id",
                       headers={**v2_tenant, "idempotency-key": "bf-ghost1"})
    d2 = client.delete("/api/resources/ghost-id",
                       headers={**v2_tenant, "idempotency-key": "bf-ghost2"})
    log_case(
        "delete never-existed",
        {"id": "ghost-id"},
        "删除从未存在的资源：稳定 200、deleted=true、不计存活数",
    )
    assert d1.status_code == d2.status_code == 200
    assert d1.json()["deleted"] is True and d2.json()["deleted"] is True
    assert app.state.resources.count("tenant-life") == 0
    assert client.get("/api/resources/ghost-id", headers=v2_tenant).status_code == 404
