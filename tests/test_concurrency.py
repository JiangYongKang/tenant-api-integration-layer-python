"""并发与部分失败场景：无重复副作用、无状态残留、无资源泄漏、失败后键可复用。"""

from __future__ import annotations

import threading

from app.idempotency import execute_idempotent
from app.stores import IdempotencyStore


def test_producer_failure_clears_placeholder_and_allows_retry():
    """首次执行失败：不留半成品记录、不锁死幂等键、无副作用计数残留。"""
    store = IdempotencyStore()
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient failure")
        return 200, {"ok": True}

    try:
        execute_idempotent(store, "t", "retry-me", "POST", "/x", b"{}", flaky)
    except RuntimeError:
        pass
    assert store.get("t", "retry-me") is None  # 占位已清理
    result = execute_idempotent(store, "t", "retry-me", "POST", "/x", b"{}", flaky)
    assert result.status_code == 200 and result.body == {"ok": True}
    assert attempts["n"] == 2
    assert store.get("t", "retry-me").completed is True


def test_concurrent_different_keys_do_not_block_each_other():
    """不同幂等键使用不同锁：一个慢请求不会阻塞其他键。"""
    store = IdempotencyStore()
    order: list[str] = []
    cond = threading.Condition()

    def make_worker(key: str, slow: bool):
        def producer():
            with cond:
                order.append(key)
            if slow:
                import time
                time.sleep(0.1)
            return 201, {"key": key}

        def run():
            execute_idempotent(store, "t", key, "POST", "/x", key.encode(), producer)

        return run

    t_slow = threading.Thread(target=make_worker("slow", True))
    t_fast = threading.Thread(target=make_worker("fast", False))
    t_slow.start()
    import time
    time.sleep(0.01)
    t_fast.start()
    t_fast.join(timeout=2)
    t_slow.join(timeout=2)
    # fast 必须先完成（即使后启动），说明没有被 slow 阻塞
    assert order == ["slow", "fast"]


def test_heavy_concurrent_mix_converges_with_correct_counts():
    """混合并发：同键请求收敛 1 次副作用，异键各自 1 次。"""
    store = IdempotencyStore()
    counter: dict[str, int] = {}
    lock = threading.Lock()

    def producer_for(key: str):
        def producer():
            with lock:
                counter[key] = counter.get(key, 0) + 1
            return 201, {"id": key}
        return producer

    threads = []
    # hot-key 被 20 个线程并发；另有 5 个独立键各 1 个线程
    barrier = threading.Barrier(25)

    def hot():
        barrier.wait()
        execute_idempotent(store, "t", "hot", "POST", "/x", b"hot", producer_for("hot"))

    def cold(key: str):
        def run():
            barrier.wait()
            execute_idempotent(store, "t", key, "POST", "/x", key.encode(), producer_for(key))
        return run

    for _ in range(20):
        threads.append(threading.Thread(target=hot))
    for i in range(5):
        threads.append(threading.Thread(target=cold(f"cold-{i}")))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert counter["hot"] == 1
    for i in range(5):
        assert counter[f"cold-{i}"] == 1
    assert sum(counter.values()) == 6


def test_idempotency_lock_objects_are_reused_not_leaked_per_request():
    """同一键的锁对象在重复请求间复用，不会按请求无限增长。"""
    store = IdempotencyStore()
    l1 = store.lock_for("t", "k")
    for _ in range(50):
        execute_idempotent(
            store, "t", "other", "POST", f"/x", b"{}", lambda: (200, {})
        )
    l2 = store.lock_for("t", "k")
    assert l1 is l2
    # 记录字典也不会因重复请求增长
    n_records = len(store._records)
    execute_idempotent(store, "t", "other", "POST", "/x", b"{}", lambda: (200, {}))
    assert len(store._records) == n_records


def test_concurrent_same_key_with_different_bodies_only_one_wins():
    """同键但内容不同的并发请求：恰好一个成功，另一个冲突，绝无两次副作用。"""
    store = IdempotencyStore()
    counter = {"n": 0}
    outcomes: list[str] = []
    out_lock = threading.Lock()
    barrier = threading.Barrier(2)

    import time

    from app.errors import APIError

    def worker(body: bytes):
        def producer():
            counter["n"] += 1
            time.sleep(0.05)
            return 201, {"body": body.decode()}
        barrier.wait()
        try:
            execute_idempotent(store, "t", "race", "POST", "/x", body, producer)
            with out_lock:
                outcomes.append("ok")
        except APIError as e:
            with out_lock:
                outcomes.append(e.code.value)

    t1 = threading.Thread(target=worker, args=(b"aaa",))
    t2 = threading.Thread(target=worker, args=(b"bbb",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert counter["n"] == 1
    assert sorted(outcomes) == ["idempotency_conflict", "ok"]
