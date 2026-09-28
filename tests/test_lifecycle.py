"""资源生命周期：更新、删除、乐观并发、幂等重放一致、跨操作隔离、版本拒绝。"""
import threading

import pytest

from integration_layer.errors import ErrorCode

from .conftest import log_case


@pytest.fixture()
def v2_tenant(app):
    key_id, secret = app.state.credentials.register(
        "tenant-life", {"resources:read", "resources:write"}
    )
    headers = {
        "x-api-key-id": key_id,
        "x-api-secret": secret,
        "x-api-version": "v2",
    }
    return headers


def _create(client, headers, name="r", key="c-1"):
    r = client.post("/api/resources", json={"name": name}, headers={**headers, "idempotency-key": key})
    assert r.status_code == 201, r.text
    # 创建响应不含生命周期字段；版本通过 GET 获取
    g = client.get(f"/api/resources/{r.json()['id']}", headers=headers)
    assert g.status_code == 200
    return g.json()


def test_update_success_bumps_version(client, v2_tenant):
    res = _create(client, v2_tenant, "alpha", "lu-1")
    r = client.patch(
        f"/api/resources/{res['id']}",
        json={"name": "alpha-2", "expected_version": res["version"]},
        headers={**v2_tenant, "idempotency-key": "lu-u1"},
    )
    log_case(
        "update ok",
        {"id": res["id"], "expected_version": 1, "new_name": "alpha-2"},
        "带正确期望版本更新应 200，version 递增，name 改变",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "alpha-2"
    assert body["version"] == 2
    assert body["updated_at"] is not None


def test_concurrent_stale_update_single_winner(client, v2_tenant):
    res = _create(client, v2_tenant, "hot", "lc-1")
    outcomes: list = []

    def worker(idx):
        outcomes.append(
            client.patch(
                f"/api/resources/{res['id']}",
                json={"name": f"n{idx}", "expected_version": 1},
                headers={**v2_tenant, "idempotency-key": f"lc-u{idx}"},
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in outcomes if r.status_code == 200]
    conflicts = [
        r for r in outcomes
        if r.status_code == 409
        and r.json()["error"]["code"] == ErrorCode.CONCURRENT_CONFLICT.value
    ]
    log_case(
        "optimistic concurrency",
        {"threads": 8, "expected_version": "all 1"},
        "同一份旧状态并发更新：恰好一个成功，其余 CONCURRENT_CONFLICT 且带当前版本",
    )
    assert len(winners) == 1
    assert len(winners) + len(conflicts) == 8
    sample = conflicts[0].json()["error"]["details"]
    assert sample["expected_version"] == 1
    assert sample["current_version"] == 2  # 让调用方看出自己手上的状态过期


def test_concurrent_conflict_distinct_from_other_errors(client, v2_tenant):
    res = _create(client, v2_tenant, "d", "ld-1")
    # 先成功更新一次制造版本差
    client.patch(
        f"/api/resources/{res['id']}",
        json={"name": "d2", "expected_version": 1},
        headers={**v2_tenant, "idempotency-key": "ld-u1"},
    )
    stale = client.patch(
        f"/api/resources/{res['id']}",
        json={"name": "d3", "expected_version": 1},
        headers={**v2_tenant, "idempotency-key": "ld-u2"},
    )
    idem = client.patch(
        f"/api/resources/{res['id']}",
        json={"name": "x", "expected_version": 2},
        headers={**v2_tenant, "idempotency-key": "ld-u1"},  # 同键不同内容
    )
    log_case(
        "conflict code separation",
        {"stale_version": 1, "idem_key_reuse": True},
        "并发冲突与幂等冲突必须是不同错误码",
    )
    assert stale.json()["error"]["code"] == ErrorCode.CONCURRENT_CONFLICT.value
    assert idem.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value


def test_update_idempotent_replay_exact(client, v2_tenant):
    res = _create(client, v2_tenant, "r", "lr-1")
    payload = {"name": "r2", "expected_version": 1}
    r1 = client.patch(
        f"/api/resources/{res['id']}", json=payload,
        headers={**v2_tenant, "idempotency-key": "lr-u1"},
    )
    r2 = client.patch(
        f"/api/resources/{res['id']}", json=payload,
        headers={**v2_tenant, "idempotency-key": "lr-u1"},
    )
    b1, b2 = r1.json(), r2.json()
    b1.pop("request_id"); b2.pop("request_id")  # request_id 逐请求变化，属预期
    log_case(
        "update replay identical",
        {"key": "lr-u1", "payload": "same"},
        "更新同键重放：状态码与所有业务响应字段逐字一致，副作用一次",
    )
    assert r1.status_code == r2.status_code == 200
    assert b1 == b2
    assert b1["version"] == 2  # 没有因为重放再递增


def test_delete_idempotent_consistent(client, v2_tenant):
    res = _create(client, v2_tenant, "r", "lx-1")
    r1 = client.delete(
        f"/api/resources/{res['id']}", headers={**v2_tenant, "idempotency-key": "lx-d1"}
    )
    r2 = client.delete(
        f"/api/resources/{res['id']}", headers={**v2_tenant, "idempotency-key": "lx-d1"}
    )
    b1, b2 = r1.json(), r2.json()
    b1.pop("request_id"); b2.pop("request_id")
    log_case(
        "delete replay identical",
        {"key": "lx-d1"},
        "删除同键重放：响应逐字一致，deleted=true",
    )
    assert r1.status_code == r2.status_code == 200
    assert b1 == b2
    assert b1["deleted"] is True
    # 换一个幂等键再删已删除资源：仍然一致、可解释，不是内部错误
    r3 = client.delete(
        f"/api/resources/{res['id']}", headers={**v2_tenant, "idempotency-key": "lx-d2"}
    )
    b3 = r3.json()
    b3.pop("request_id")
    log_case(
        "delete already-deleted",
        {"new_key": "lx-d2"},
        "已删资源用新键再删：200 且与首次删除响应字段逐字一致，不产生内部错误",
    )
    assert r3.status_code == 200
    assert r3.json()["deleted"] is True
    assert b3 == b1  # 跨幂等键的重复删除结果也一致
    # GET 已删资源 -> 404（无状态残留成一个活资源）
    assert client.get(f"/api/resources/{res['id']}", headers=v2_tenant).status_code == 404


def test_delete_with_stale_if_match_conflicts(client, v2_tenant):
    res = _create(client, v2_tenant, "r", "lm-1")
    client.patch(
        f"/api/resources/{res['id']}",
        json={"name": "r2", "expected_version": 1},
        headers={**v2_tenant, "idempotency-key": "lm-u1"},
    )
    r = client.delete(
        f"/api/resources/{res['id']}",
        headers={**v2_tenant, "idempotency-key": "lm-d1", "if-match": "1"},
    )
    log_case(
        "delete stale version",
        {"if-match": 1, "current": 2},
        "携带过期 If-Match 删除：CONCURRENT_CONFLICT，资源未被删",
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == ErrorCode.CONCURRENT_CONFLICT.value
    assert client.get(f"/api/resources/{res['id']}", headers=v2_tenant).status_code == 200


def test_cross_operation_same_idempotency_key_isolated(client, v2_tenant):
    res = _create(client, v2_tenant, "r", "lo-1")
    # 同一幂等键先用于创建（成功），再用于更新；两者命名空间隔离，互不串扰
    r_upd = client.patch(
        f"/api/resources/{res['id']}",
        json={"name": "r2", "expected_version": 1},
        headers={**v2_tenant, "idempotency-key": "lo-1"},
    )
    r_del = client.delete(
        f"/api/resources/{res['id']}", headers={**v2_tenant, "idempotency-key": "lo-1"}
    )
    log_case(
        "cross-op key isolation",
        {"same_key": "lo-1", "ops": ["create", "update", "delete"]},
        "同一幂等键跨操作各算各的：更新与删除均正常执行，不报幂等冲突",
    )
    assert r_upd.status_code == 200, r_upd.text
    assert r_del.status_code == 200, r_del.text


def test_v1_cannot_use_lifecycle(client, tenant):
    """旧版本不具备 resource_lifecycle 能力：更新/删除明确拒绝，不静默降级。"""
    _, v1_headers = tenant
    # v1 先创建一个资源
    r = client.post(
        "/api/resources", json={"name": "r"},
        headers={**v1_headers, "idempotency-key": "lv-c1"},
    )
    assert r.status_code == 201
    rid = r.json()["id"]
    rp = client.patch(
        f"/api/resources/{rid}",
        json={"name": "r2", "expected_version": 1},
        headers={**v1_headers, "idempotency-key": "lv-u1"},
    )
    rd = client.delete(
        f"/api/resources/{rid}", headers={**v1_headers, "idempotency-key": "lv-d1"}
    )
    log_case(
        "v1 lifecycle rejected",
        {"version": "v1", "ops": ["PATCH", "DELETE"]},
        "旧版本使用生命周期操作必须 UNSUPPORTED_CAPABILITY，绝不静默降级",
    )
    assert rp.status_code == 400
    assert rp.json()["error"]["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value
    assert rd.status_code == 400
    assert rd.json()["error"]["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value


def test_update_missing_expected_version_rejected(client, v2_tenant):
    res = _create(client, v2_tenant, "r", "lme-1")
    r = client.patch(
        f"/api/resources/{res['id']}", json={"name": "r2"},
        headers={**v2_tenant, "idempotency-key": "lme-u1"},
    )
    log_case(
        "update missing version",
        {"payload": {"name": "r2"}},
        "更新缺 expected_version 应 VALIDATION_FAILED（参数错误，非冲突）",
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value


def test_update_unknown_resource_404(client, v2_tenant):
    r = client.patch(
        "/api/resources/nope", json={"name": "x", "expected_version": 1},
        headers={**v2_tenant, "idempotency-key": "ln-u1"},
    )
    log_case("update missing resource", {"id": "nope"}, "更新不存在资源应 NOT_FOUND")
    assert r.status_code == 404


def test_concurrent_delete_same_resource_single_winner(client, v2_tenant):
    res = _create(client, v2_tenant, "hot-del", "lcd-1")
    outcomes: list = []

    def worker(idx):
        outcomes.append(
            client.delete(
                f"/api/resources/{res['id']}",
                headers={**v2_tenant, "idempotency-key": f"lcd-d{idx}", "if-match": "1"},
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    deleted_views = [
        r for r in outcomes
        if r.status_code == 200 and r.json().get("deleted") is True
    ]
    log_case(
        "concurrent delete",
        {"threads": 8, "if-match": "all 1"},
        "并发删除收敛到同一墓碑：全部 200、version 恒为 2（恰好删一次，不重复推进）",
    )
    # 删除幂等：全部得到一致的已删视图；墓碑版本停在 2，证明副作用只发生一次
    assert len(deleted_views) == 8
    assert {r.json()["version"] for r in deleted_views} == {2}
    g = client.get(f"/api/resources/{res['id']}", headers=v2_tenant)
    assert g.status_code == 404


def test_get_v2_exposes_version_and_hides_for_v1(client, tenant, v2_tenant):
    res = _create(client, v2_tenant, "proj", "lp-1")
    g2 = client.get(f"/api/resources/{res['id']}", headers=v2_tenant)
    # 用 v1 凭证另建并读，验证旧版本响应不含生命周期字段
    _, v1 = tenant
    c1 = client.post("/api/resources", json={"name": "old"},
                     headers={**v1, "idempotency-key": "lp-v1c"})
    g1 = client.get(f"/api/resources/{c1.json()['id']}", headers=v1)
    log_case(
        "version projection",
        {"v2_fields": "version/updated_at present", "v1_fields": "absent"},
        "生命周期字段只在支持它的 v2 响应出现，v1 响应裁剪",
    )
    assert "version" in g2.json() and "updated_at" in g2.json()
    assert "version" not in g1.json() and "updated_at" not in g1.json()
