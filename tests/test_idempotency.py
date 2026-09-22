"""幂等语义测试：同键同内容收敛、同键异内容拒绝、并发只执行一次、跨租户隔离。"""

from __future__ import annotations

import threading

from tests.conftest import assert_error, create_widget


def _post(env, key, body, tenant="tenant-a", label=None, version=1):
    label = label or ("writer" if tenant == "tenant-b" else "ci-writer")
    return env.client.post(
        f"/api/v{version}/widgets",
        json=body,
        headers={**env.auth(tenant, label), "Idempotency-Key": key},
    )


def test_duplicate_returns_identical_result_without_extra_side_effect(env):
    r1 = _post(env, "order-1", {"name": "a", "data": {"v": 1}})
    assert r1.status_code == 201
    assert r1.headers.get("idempotent-replay") is None
    r2 = _post(env, "order-1", {"name": "a", "data": {"v": 1}})
    assert r2.status_code == 201
    assert r2.headers["idempotent-replay"] == "true"
    assert r1.json() == r2.json()
    assert env.deps.resources.side_effect_count == 1


def test_same_key_different_content_is_rejected(env):
    _post(env, "order-2", {"name": "a"})
    r = _post(env, "order-2", {"name": "different"})
    err = assert_error(r, 409, "idempotency_conflict")
    assert err["details"]["reason"] == "request_content_mismatch"
    assert env.deps.resources.side_effect_count == 1


def test_concurrent_duplicate_submissions_converge(env):
    """同一幂等键并发 N 次：仅一次副作用，全部得到同一结果。"""
    results: list = []
    errors: list = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(_post(env, "concurrent-key", {"name": "c"}))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 8
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    bodies = {r.text for r in results}
    assert len(bodies) == 1  # 响应完全收敛一致
    assert sum(1 for r in results if r.headers.get("idempotent-replay") == "true") == 7
    assert env.deps.resources.side_effect_count == 1


def test_keys_scoped_per_tenant(env):
    r1 = _post(env, "shared-key", {"name": "a"}, tenant="tenant-a")
    r2 = _post(env, "shared-key", {"name": "a"}, tenant="tenant-b")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
    assert env.deps.resources.side_effect_count == 2


def test_idempotency_key_required_for_write(env):
    r = env.client.post(
        "/api/v1/widgets", json={"name": "a"}, headers=env.auth("tenant-a", "ci-writer")
    )
    err = assert_error(r, 422, "validation_failed")
    assert err["details"]["header"] == "Idempotency-Key"


def test_idempotency_key_length_and_charset_limited(env):
    r = _post(env, "x" * 201, {"name": "a"})
    assert_error(r, 422, "validation_failed")
    r = _post(env, "bad key", {"name": "a"})
    assert_error(r, 422, "validation_failed")


def test_replay_logged_with_decision(env):
    _post(env, "logged-key", {"name": "a"})
    _post(env, "logged-key", {"name": "a"})
    replays = env.logs("idempotent_replay")
    assert len(replays) == 1
    assert replays[0]["decision"] == "replay"
    assert replays[0]["input_idempotency_key"] == "logged-key"
    # 只有一次真实副作用日志
    creates = env.logs("widget_created")
    assert len(creates) == 1
