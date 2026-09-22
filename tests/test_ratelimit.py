"""按租户与接口维度的限流测试。"""

from __future__ import annotations

from tests.conftest import assert_error


def _write(client, token, key, version=1, name="n"):
    return client.post(
        f"/api/v{version}/widgets",
        json={"name": name},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )


def test_tenant_b_write_quota_exhausted_with_retry_hint(env):
    token = env.raw_token("tenant-b", "writer")
    statuses = [_write(env.client, token, f"k{i}").status_code for i in range(5)]
    assert statuses == [201, 201, 201, 429, 429], statuses
    r = _write(env.client, token, "k-again")
    err = assert_error(r, 429, "rate_limited")
    assert err["details"]["limit"] == 3
    assert err["details"]["window_ms"] == 1000
    assert err["details"]["retry_after_ms"] >= 1
    assert int(r.headers["retry-after-ms"]) == err["details"]["retry_after_ms"]


def test_rate_limits_are_isolated_per_tenant(env):
    token_b = env.raw_token("tenant-b", "writer")
    # 打满 tenant-b
    for i in range(4):
        _write(env.client, token_b, f"b{i}")
    # tenant-a 的同接口完全不受影响
    token_a = env.raw_token("tenant-a", "ci-writer")
    r = _write(env.client, token_a, "a1")
    assert r.status_code == 201, r.text


def test_rate_limits_are_isolated_per_endpoint(env):
    """tenant-b 写接口被限流，但读接口走独立窗口不受影响。"""
    token_b = env.raw_token("tenant-b", "writer")
    # 先用掉 3 次写配额，其中保留一个可读资源
    created = _write(env.client, token_b, "readable", name="readme")
    assert created.status_code == 201
    _write(env.client, token_b, "w1")
    _write(env.client, token_b, "w2")
    assert _write(env.client, token_b, "w3").status_code == 429
    # 读取使用 GET 独立窗口
    r = env.client.get(
        f"/api/v1/widgets/{created.json()['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r.status_code == 200, r.text


def test_window_rolls_over_using_clock(env):
    token = env.raw_token("tenant-b", "writer")
    for i in range(4):
        _write(env.client, token, f"t{i}")
    assert _write(env.client, token, "blocked").status_code == 429
    # 推进时钟超过窗口长度
    env.clock.advance_ms(1001)
    r = _write(env.client, token, "after-window")
    assert r.status_code == 201, r.text


def test_v1_and_v2_write_endpoints_have_independent_windows(env):
    token = env.raw_token("tenant-b", "writer")
    # v1 打满 3 次
    for i in range(4):
        _write(env.client, token, f"v1-{i}", version=1)
    # v2 是不同 endpoint_key，窗口独立
    r = _write(env.client, token, "v2-1", version=2)
    assert r.status_code == 201, r.text
