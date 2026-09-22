"""大请求体与超限输入：可配置上限、稳定拒绝、无状态残留、无键卡死。"""

from __future__ import annotations

from tests.conftest import assert_error


def test_global_hard_limit_rejects_huge_body_during_read(env):
    """超过全局默认上限（64KiB）在 ASGI 读取阶段即被拒绝（413）。"""
    token = env.raw_token("tenant-a", "ci-writer")
    huge = {"name": "x", "data": {"pad": "A" * (70 * 1024)}}
    r = env.client.post(
        "/api/v2/widgets",
        json=huge,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "huge"},
    )
    err = assert_error(r, 413, "payload_too_large")
    assert err["details"]["max_body_bytes"] == 65_536
    assert err["details"]["observed_bytes"] > 65_536


def test_tenant_specific_smaller_limit_is_enforced(env):
    token = env.raw_token("tenant-c", "tiny-writer")
    body = {"name": "x", "data": {"pad": "A" * 300}}  # >256 字节、远小于全局上限
    r = env.client.post(
        "/api/v2/widgets",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "small"},
    )
    err = assert_error(r, 413, "payload_too_large")
    assert err["details"]["max_body_bytes"] == 256
    assert err["details"]["detected_via"] == "tenant_limit"
    # 超限请求不产生副作用、不占资源
    assert env.deps.resources.side_effect_count == 0


def test_field_level_limits_are_stable(env):
    token = env.raw_token("tenant-a", "ci-writer")
    cases = [
        ({"name": "x" * 201}, "name"),
        ({"name": 1}, "name"),
        ({"name": "x", "data": []}, "data"),
        ({"name": "x", "channel": ""}, "channel"),
        ({"name": "x", "unknown_field": 1}, "unknown_field"),
        ({}, "name"),
    ]
    for body, _frag in cases:
        r = env.client.post(
            "/api/v2/widgets",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"f{_frag}-{len(str(body))}"},
        )
        assert_error(r, 422, "validation_failed")


def test_invalid_json_body_returns_validation_contract(env):
    token = env.raw_token("tenant-a", "ci-writer")
    r = env.client.post(
        "/api/v1/widgets",
        content=b"{not-json",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "bad-json",
            "Content-Type": "application/json",
        },
    )
    err = assert_error(r, 422, "validation_failed")
    assert err["details"]["fields"][0]["rule"] == "invalid_json"


def test_oversized_request_does_not_lock_idempotency_key(env):
    """被 413 拒绝的请求发生在幂等处理之前，键可被后续合法请求复用。"""
    token = env.raw_token("tenant-c", "tiny-writer")
    big = {"name": "x", "data": {"pad": "A" * 300}}
    r1 = env.client.post(
        "/api/v2/widgets", json=big,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "reuse-me"},
    )
    assert r1.status_code == 413
    r2 = env.client.post(
        "/api/v2/widgets", json={"name": "ok"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "reuse-me"},
    )
    assert r2.status_code == 201, r2.text
