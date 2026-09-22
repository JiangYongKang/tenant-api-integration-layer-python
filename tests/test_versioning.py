"""版本协商与兼容规则测试。

覆盖：两个版本同时可用、新版本加法兼容、旧版本携带新字段被拒、
不存在的版本、不支持的能力、Accept-Version 冲突，且日志记录判定依据。
"""

from __future__ import annotations

from tests.conftest import assert_error, create_widget


def test_v1_and_v2_served_simultaneously(env):
    r1 = create_widget(env, key="v1-key", version=1, body={"name": "old"})
    assert r1.status_code == 201, r1.text
    b1 = r1.json()
    assert b1["created_at"] and "created_at_iso" not in b1 and "channel" not in b1

    r2 = create_widget(env, key="v2-key", version=2, body={"name": "new", "channel": "web"})
    assert r2.status_code == 201, r2.text
    b2 = r2.json()
    # 新字段全部是"加法"：v2 同时保留 v1 全部字段且语义不变
    assert b2["created_at"] == b1["created_at"]
    assert b2["created_at_iso"] and b2["channel"] == "web"


def test_v2_is_backward_compatible_with_v1_payload(env):
    """v2 接受 v1 形状请求（新字段可选，缺省稳定）。"""
    r = create_widget(env, key="compat", version=2, body={"name": "plain"})
    assert r.status_code == 201
    assert r.json()["channel"] is None and r.json()["created_at_iso"]


def test_unknown_version_is_explicitly_rejected(env):
    r = env.client.post(
        "/api/v9/widgets", json={"name": "x"},
        headers={**env.auth("tenant-a", "ci-writer"), "Idempotency-Key": "k"},
    )
    err = assert_error(r, 404, "version_not_found")
    assert err["details"]["supported"] == [1, 2]


def test_v1_sending_v2_only_field_is_incompatible(env):
    r = create_widget(
        env, version=1, key="bad-field", body={"name": "x", "channel": "app"}
    )
    err = assert_error(r, 400, "incompatible_request")
    # 必须明确指出是哪个字段、从哪个版本开始支持
    assert err["details"]["unsupported_fields"] == ["channel"]
    assert err["details"]["since_version"] == 2


def test_capability_not_existing_on_old_version(env):
    """v1 上访问 v2 独有能力路径 -> unsupported_capability（不是静默 404 降级）。"""
    r = env.client.get(
        "/api/v1/widgets/abc/insights", headers=env.auth("tenant-a", "ci-writer")
    )
    assert_error(r, 404, "unsupported_capability")


def test_accept_version_conflict_is_rejected(env):
    r = create_widget(
        env, version=1, key="av1", body={"name": "x"},
        extra_headers={"Accept-Version": "v2"},
    )
    err = assert_error(r, 400, "incompatible_request")
    assert err["details"]["path_version"] == 1
    assert err["details"]["accept_version"] == 2


def test_accept_version_matches_path_passes(env):
    r = create_widget(
        env, version=2, key="av2", body={"name": "x"},
        extra_headers={"Accept-Version": "2"},
    )
    assert r.status_code == 201, r.text


def test_accept_version_unknown_is_rejected(env):
    r = create_widget(
        env, version=2, key="av3", body={"name": "x"},
        extra_headers={"Accept-Version": "v77"},
    )
    assert_error(r, 404, "version_not_found")


def test_rejection_logged_with_input_and_decision(env):
    create_widget(
        env, version=1, key="log-case", body={"name": "x", "channel": "web"}
    )
    rejected = env.logs("request_rejected")
    assert any(
        e.get("reason") == "incompatible_request"
        and e.get("decision") == "reject"
        and e.get("path") == "/api/v1/widgets"
        for e in rejected
    )
