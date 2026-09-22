"""幂等写入：重放一致、键复用拒绝、并发重复提交收敛、副作用仅一次。"""
import threading

from integration_layer.errors import ErrorCode

from .conftest import log_case


def _post(client, headers, name, key):
    return client.post(
        "/api/resources",
        json={"name": name},
        headers={**headers, "idempotency-key": key},
    )


def test_replay_returns_same_result_single_side_effect(app, client, tenant):
    tenant_id, headers = tenant
    r1 = _post(client, headers, "alpha", "k-1")
    r2 = _post(client, headers, "alpha", "k-1")
    log_case(
        "idempotent replay",
        {"key": "k-1", "payload": "same"},
        "相同键+相同内容：结果一致、副作用一次、第二次为 200 重放",
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert app.state.resources.count(tenant_id) == 1


def test_same_key_different_payload_rejected(app, client, tenant):
    tenant_id, headers = tenant
    r1 = _post(client, headers, "alpha", "k-2")
    r2 = _post(client, headers, "beta", "k-2")
    log_case(
        "idempotency key reuse",
        {"key": "k-2", "payloads": ["alpha", "beta"]},
        "相同键+不同内容：必须 IDEMPOTENCY_CONFLICT 且不产生第二次副作用",
    )
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value
    assert app.state.resources.count(tenant_id) == 1


def test_concurrent_duplicate_submit_converges(app, client, tenant):
    tenant_id, headers = tenant
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(_post(client, headers, "gamma", "k-3"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_case(
        "concurrent duplicate submit",
        {"threads": 16, "key": "k-3"},
        "并发同键同内容：全部收敛到同一资源 id，副作用恰好一次",
    )
    assert not errors
    ids = {r.json()["id"] for r in results}
    statuses = {r.status_code for r in results}
    assert len(ids) == 1
    assert statuses <= {200, 201}
    assert statuses == {200, 201}  # 恰好一个创建，其余重放
    assert app.state.resources.count(tenant_id) == 1


def test_concurrent_same_key_different_payload_single_winner(app, client, tenant):
    tenant_id, headers = tenant
    outcomes: list = []

    def worker(name):
        outcomes.append(_post(client, headers, name, "k-4"))

    threads = [
        threading.Thread(target=worker, args=(f"name-{i}",)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_case(
        "concurrent conflicting payloads",
        {"threads": 8, "key": "k-4"},
        "并发同键不同内容：至多一个成功，其余全部冲突，无副作用残留",
    )
    created = [r for r in outcomes if r.status_code == 201]
    conflicts = [
        r
        for r in outcomes
        if r.status_code == 409
        and r.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value
    ]
    assert len(created) == 1
    assert len(created) + len(conflicts) == len(outcomes)
    assert app.state.resources.count(tenant_id) == 1


def test_missing_idempotency_key_rejected(client, tenant):
    _, headers = tenant
    r = client.post("/api/resources", json={"name": "x"}, headers=headers)
    log_case("missing idempotency key", {"headers": "no Idempotency-Key"}, "写请求缺幂等键应为 VALIDATION_FAILED")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value
