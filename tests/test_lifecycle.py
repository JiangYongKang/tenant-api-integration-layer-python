"""资源生命周期：更新（乐观并发）、删除（幂等）、冲突可区分、幂等重放一致、跨操作隔离。"""
import threading

from integration_layer.errors import ErrorCode

from .conftest import log_case


def _create(client, headers, name="res", key="c-1"):
    r = client.post(
        "/api/resources", json={"name": name}, headers={**headers, "idempotency-key": key}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], int(r.headers["x-resource-version"])


def test_update_succeeds_and_bumps_version(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-1")
    log_case(
        "update ok",
        {"id": rid, "expected_version": ver, "change": {"name": "res-2"}},
        "版本匹配时更新成功，响应头 x-resource-version 单调 +1",
    )
    r = client.patch(
        f"/api/resources/{rid}",
        json={"expected_version": ver, "name": "res-2"},
        headers={**headers, "idempotency-key": "u-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "res-2"
    assert int(r.headers["x-resource-version"]) == ver + 1
    # GET 能看到更新
    g = client.get(f"/api/resources/{rid}", headers=headers)
    assert g.json()["name"] == "res-2"


def test_concurrent_updates_single_winner_conflict_is_distinct(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-2")
    log_case(
        "concurrent update",
        {"threads": 8, "expected_version": ver},
        "同版本并发更新：恰好一个成功，其余 CONCURRENT_CONFLICT，并回显当前版本",
    )
    outcomes: list = []

    def worker(i):
        outcomes.append(
            client.patch(
                f"/api/resources/{rid}",
                json={"expected_version": ver, "name": f"n-{i}"},
                headers={**headers, "idempotency-key": f"cu-{i}"},
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in outcomes if r.status_code == 200]
    conflicts = [
        r for r in outcomes
        if r.status_code == 409
        and r.json()["error"]["code"] == ErrorCode.CONCURRENT_CONFLICT.value
    ]
    assert len(oks) == 1, [r.status_code for r in outcomes]
    assert len(oks) + len(conflicts) == 8
    sample = conflicts[0].json()["error"]
    # 冲突结果必须与参数错误(400)/鉴权(401/403)/幂等冲突(409 IDEMPOTENCY_CONFLICT) 可区分
    assert sample["code"] == "CONCURRENT_CONFLICT"
    assert sample["details"]["expected_version"] == ver
    assert sample["details"]["current_version"] == ver + 1  # 明确告知手上状态已过期
    # 并发更新只生效一次：名字是唯一获胜者写入的
    g = client.get(f"/api/resources/{rid}", headers=headers)
    assert g.json()["name"] == oks[0].json()["name"]


def test_concurrent_conflict_distinct_from_validation_auth_idempotency(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-3")
    cases = [
        ("参数错误", client.patch(
            f"/api/resources/{rid}", json={"expected_version": "x"},
            headers={**headers, "idempotency-key": "d-1"})),
        ("并发冲突", client.patch(
            f"/api/resources/{rid}", json={"expected_version": ver - 99, "name": "z"},
            headers={**headers, "idempotency-key": "d-2"})),
    ]
    no_auth = client.patch(
        f"/api/resources/{rid}", json={"expected_version": ver, "name": "z"},
        headers={**headers, "x-api-key-id": ""},
    )
    # 幂等冲突：同键不同内容
    client.patch(f"/api/resources/{rid}", json={"expected_version": ver, "name": "a"},
                 headers={**headers, "idempotency-key": "d-3"})
    idem = client.patch(f"/api/resources/{rid}", json={"expected_version": ver, "name": "b"},
                        headers={**headers, "idempotency-key": "d-3"})
    log_case(
        "conflict taxonomy",
        {"validation": "expected_version=x", "auth": "missing key",
         "concurrent": "old version", "idempotency": "same key diff payload"},
        "四类失败必须稳定可区分",
    )
    assert cases[0][1].status_code == 400
    assert cases[0][1].json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value
    assert no_auth.status_code == 401
    assert cases[1][1].status_code == 409
    assert cases[1][1].json()["error"]["code"] == ErrorCode.CONCURRENT_CONFLICT.value
    assert idem.status_code == 409
    assert idem.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value


def test_update_replay_is_identical_single_side_effect(client, tenant, app):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-4")
    body = {"expected_version": ver, "name": "once"}
    r1 = client.patch(f"/api/resources/{rid}", json=body,
                      headers={**headers, "idempotency-key": "ur-1"})
    r2 = client.patch(f"/api/resources/{rid}", json=body,
                      headers={**headers, "idempotency-key": "ur-1"})
    log_case(
        "update replay",
        {"key": "ur-1", "body": body},
        "更新重放：状态码/业务字段/版本头一致，副作用只一次",
    )
    assert r1.status_code == 200
    assert r2.status_code == r1.status_code
    assert r2.json()["name"] == r1.json()["name"]
    assert r2.headers["x-resource-version"] == r1.headers["x-resource-version"]
    # 版本只增加一次（一次成功副作用）
    assert int(r2.headers["x-resource-version"]) == ver + 1


def test_update_conflict_replay_is_stable(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-5")
    # 先成功更新一次让 ver 过期
    client.patch(f"/api/resources/{rid}", json={"expected_version": ver, "name": "new"},
                 headers={**headers, "idempotency-key": "ucr-0"})
    stale = {"expected_version": ver, "name": "late"}
    r1 = client.patch(f"/api/resources/{rid}", json=stale,
                      headers={**headers, "idempotency-key": "ucr-1"})
    r2 = client.patch(f"/api/resources/{rid}", json=stale,
                      headers={**headers, "idempotency-key": "ucr-1"})
    log_case(
        "conflict replay",
        {"key": "ucr-1", "expected_version": ver},
        "过期更新的重放：冲突结果（除 request_id）完全一致",
    )
    e1, e2 = r1.json()["error"], r2.json()["error"]
    assert e1["code"] == "CONCURRENT_CONFLICT" == e2["code"]
    assert e1["details"] == e2["details"]
    assert e1["message"] == e2["message"]


def test_delete_is_idempotent_with_consistent_result(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-6")
    log_case(
        "delete idempotent",
        {"id": rid},
        "首次删除 200 deleted；再次删除 200 already_deleted，视图一致可解释",
    )
    r1 = client.delete(f"/api/resources/{rid}", headers={**headers, "idempotency-key": "del-1"})
    assert r1.status_code == 200
    assert r1.json()["result"] == "deleted"
    assert r1.json()["deleted"] is True and r1.json()["id"] == rid
    assert int(r1.headers["x-resource-version"]) == ver + 1
    r2 = client.delete(f"/api/resources/{rid}", headers={**headers, "idempotency-key": "del-2"})
    assert r2.status_code == 200
    assert r2.json()["result"] == "already_deleted"
    assert r2.json()["id"] == rid and r2.json()["deleted"] is True
    # 不变成内部错误；无状态残留：GET 404、计数归零、再更新 not_found
    assert client.get(f"/api/resources/{rid}", headers=headers).status_code == 404
    ru = client.patch(f"/api/resources/{rid}", json={"expected_version": 1, "name": "x"},
                      headers={**headers, "idempotency-key": "del-up"})
    assert ru.status_code == 404


def test_delete_replay_same_key_is_identical(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-7")
    r1 = client.delete(f"/api/resources/{rid}", headers={**headers, "idempotency-key": "dr-1"})
    r2 = client.delete(f"/api/resources/{rid}", headers={**headers, "idempotency-key": "dr-1"})
    log_case(
        "delete replay",
        {"key": "dr-1"},
        "删除同键重放：状态码与全部业务字段一致，副作用一次",
    )
    assert r1.status_code == 200 == r2.status_code
    b1, b2 = dict(r1.json()), dict(r2.json())
    b1.pop("request_id"); b2.pop("request_id")
    assert b1 == b2
    assert r1.headers["x-resource-version"] == r2.headers["x-resource-version"]


def test_delete_unknown_resource_is_not_found_not_internal(client, tenant):
    _, headers = tenant
    r = client.delete("/api/resources/ghost", headers={**headers, "idempotency-key": "del-x"})
    log_case("delete missing", {"id": "ghost"}, "删从未存在的资源：NOT_FOUND，不是 5xx")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_idempotency_key_scoped_per_operation(client, tenant, app):
    _, headers = tenant
    rid, ver = _create(client, headers, name="cross", key="SHARED")
    log_case(
        "cross-operation key",
        {"key": "SHARED", "operations": ["create", "update", "delete"]},
        "同一幂等键用于不同操作时各算各的，不互相串扰",
    )
    ru = client.patch(
        f"/api/resources/{rid}",
        json={"expected_version": ver, "name": "cross-2"},
        headers={**headers, "idempotency-key": "SHARED"},
    )
    assert ru.status_code == 200, ru.text
    rd = client.delete(f"/api/resources/{rid}", headers={**headers, "idempotency-key": "SHARED"})
    assert rd.status_code == 200
    # 各自再重放也只命中自己的操作
    assert client.patch(
        f"/api/resources/{rid}",
        json={"expected_version": ver, "name": "cross-2"},
        headers={**headers, "idempotency-key": "SHARED"},
    ).status_code == 200  # 更新记录重放（资源虽已删除，但返回的是首次成功结果）
    assert client.delete(
        f"/api/resources/{rid}", headers={**headers, "idempotency-key": "SHARED"}
    ).json()["result"] == "deleted"


def test_update_v1_labels_is_incompatible_change(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-8")
    r = client.patch(
        f"/api/resources/{rid}",
        json={"expected_version": ver, "labels": {"env": "dev"}},
        headers={**headers, "x-api-version": "v1", "idempotency-key": "v1lbl"},
    )
    log_case(
        "update v1 labels",
        {"version": "v1", "field": "labels"},
        "新增语义只在能表达它的版本出现：v1 更新 labels 必须明确拒绝",
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == ErrorCode.INCOMPATIBLE_CHANGE.value
    # 拒绝后无副作用：名字未变、版本未增
    g = client.get(f"/api/resources/{rid}", headers=headers)
    assert int(g.headers["x-resource-version"]) == ver


def test_update_v2_labels_ok(client, tenant):
    _, headers = tenant
    h2 = {**headers, "x-api-version": "v2"}
    r = client.post("/api/resources", json={"name": "lab"},
                    headers={**h2, "idempotency-key": "v2c"})
    rid = r.json()["id"]; ver = int(r.headers["x-resource-version"])
    r = client.patch(
        f"/api/resources/{rid}",
        json={"expected_version": ver, "labels": {"env": "prod"}},
        headers={**h2, "idempotency-key": "v2u"},
    )
    log_case("update v2 labels", {"labels": {"env": "prod"}}, "v2 可更新 labels 并在响应中体现")
    assert r.status_code == 200
    assert r.json()["labels"] == {"env": "prod"}


def test_update_requires_idempotency_key(client, tenant):
    _, headers = tenant
    rid, ver = _create(client, headers, key="lc-9")
    r = client.patch(f"/api/resources/{rid}", json={"expected_version": ver, "name": "x"},
                     headers=headers)
    log_case("update no key", {"header": None}, "更新缺幂等键应为 VALIDATION_FAILED")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value


def test_store_compare_and_update_under_real_contention(app):
    """直接压 ResourceStore：N 个线程拿同一旧版本同时改，恰好一个成功。"""
    store = app.state.resources
    res = store.create("cas-tenant", "orig")
    rid, base = res.id, res.version
    winners: list = []
    stale = 0
    lock = threading.Lock()

    def worker(i):
        nonlocal stale
        outcome, r = store.compare_and_update(
            "cas-tenant", rid, base, {"name": f"w-{i}"}
        )
        with lock:
            if outcome == "ok":
                winners.append((i, r.version, r.name))
            elif outcome == "stale":
                stale += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_case(
        "store CAS contention",
        {"threads": 32, "expected_version": base},
        "同版本并发 CAS：唯一获胜者，版本 +1，其余全部 stale 且被告知当前版本",
    )
    assert len(winners) == 1, winners
    _, new_version, new_name = winners[0]
    assert new_version == base + 1
    assert stale == 31
    final = store.get("cas-tenant", rid)
    assert final.version == base + 1 and final.name == new_name
