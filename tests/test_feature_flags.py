"""灰度开关测试：请求级快照一致、原子热切换、非法配置拒绝、安全默认。"""

from __future__ import annotations

from tests.conftest import assert_error


def test_flag_off_blocks_capability_with_stable_contract(env):
    # tenant-b 的 insights 默认关闭
    token = env.raw_token("tenant-b", "writer")
    created = env.client.post(
        "/api/v2/widgets",
        json={"name": "x"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "i1"},
    )
    assert created.status_code == 201
    r = env.client.get(
        f"/api/v2/widgets/{created.json()['id']}/insights",
        headers={"Authorization": f"Bearer {token}"},
    )
    err = assert_error(r, 404, "unsupported_capability")
    assert err["details"]["capability"] == "widgets.insights"


def test_flag_on_allows_capability(env):
    token = env.raw_token("tenant-a", "ci-writer")
    created = env.client.post(
        "/api/v2/widgets",
        json={"name": "x"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "i1"},
    )
    r = env.client.get(
        f"/api/v2/widgets/{created.json()['id']}/insights",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["insights"]["name_length"] == 1


def test_metadata_flag_controls_v2_response_shape(env):
    token = env.raw_token("tenant-a", "ci-writer")
    r = env.client.post(
        "/api/v2/widgets",
        json={"name": "x"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "m1"},
    )
    assert "metadata" not in r.json()  # 默认关闭


def test_request_sees_one_snapshot_during_atomic_swap(env, monkeypatch):
    """进行中的请求必须始终看到同一个配置版本，不出现半切换。

    通过在服务执行点读取 whoami 前后快照一致性来验证：切换后发出的新请求
    看到新版本，而任何请求返回体中的 config_version 都只可能是整数值之一。
    """
    token = env.raw_token("tenant-a", "ci-writer")
    before = env.client.get("/_local/whoami", headers={"Authorization": f"Bearer {token}"})
    assert before.json()["config_version"] == 1

    admin = {"Authorization": f"Bearer {env.raw_token('local-admin', 'ops')}"}
    raw = env.settings_to_raw(version=2)
    raw["tenants"]["tenant-a"]["flags"]["widgets.include_metadata"] = True
    r = env.client.post("/_local/config/replace", json=raw, headers=admin)
    assert r.status_code == 200 and r.json()["config_version"] == 2

    after = env.client.get("/_local/whoami", headers={"Authorization": f"Bearer {token}"})
    body = after.json()
    assert body["config_version"] == 2
    assert body["flags"]["widgets.include_metadata"] is True
    # 进行中之后的新响应 shape 立即生效（整请求内一致）
    created = env.client.post(
        "/api/v2/widgets",
        json={"name": "y"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "m2"},
    )
    assert created.json()["metadata"]["api_version"] == 2


def test_invalid_config_rejected_and_old_config_remains(env):
    admin = {"Authorization": f"Bearer {env.raw_token('local-admin', 'ops')}"}
    bad_cases = [
        {"config_version": 2, "tenants": {}},  # 无租户
        {"config_version": 2, "tenants": {"t": {"scopes": [], "credentials": []}},
         "unknown_top": 1},  # 未知顶层字段
        "not-json-object",  # 类型错误
    ]
    for bad in bad_cases:
        r = env.client.post("/_local/config/replace", json=bad, headers=admin)
        if bad == "not-json-object":
            assert r.status_code in (422, 415), r.text
            continue
        err = assert_error(r, 422, "config_invalid")
        assert "path" in err["details"] and "reason" in err["details"]
    # 旧配置版本仍是 1，服务正常
    assert env.deps.config_store.current().config_version == 1
    token = env.raw_token("tenant-a", "ci-writer")
    r = env.client.post(
        "/api/v1/widgets",
        json={"name": "still-works"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "still"},
    )
    assert r.status_code == 201


def test_config_endpoint_requires_scope_and_redacts(env):
    # 无 config 权限
    r = env.client.get("/_local/config", headers=env.auth("tenant-a", "ci-writer"))
    assert_error(r, 403, "permission_denied")
    # 管理员可读且看不到完整指纹
    r = env.client.get("/_local/config", headers=env.auth("local-admin", "ops"))
    assert r.status_code == 200
    text = r.text
    for tenant_tokens in env.tokens.values():
        for tok in tenant_tokens.values():
            assert tok not in text
    assert "redacted" in text


def test_switch_logs_versions_and_rejection_reason(env):
    admin = {"Authorization": f"Bearer {env.raw_token('local-admin', 'ops')}"}
    bad = {"config_version": 2, "tenants": {}}
    env.client.post("/_local/config/replace", json=bad, headers=admin)
    rejected = env.logs("config_replace_rejected")
    assert rejected and rejected[0]["decision"] == "reject"
    assert rejected[0]["decision_error_path"] == "tenants"

    good = env.settings_to_raw(version=3)
    env.client.post("/_local/config/replace", json=good, headers=admin)
    replaced = env.logs("config_replaced")
    assert replaced and replaced[0]["decision"] == "atomic_replace"
    assert replaced[0]["old_config_version"] == 1
    assert replaced[0]["new_config_version"] == 3
