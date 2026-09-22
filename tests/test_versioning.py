"""版本化：多版本并存、兼容规则、明确拒绝不支持的版本/能力/变更。"""
from integration_layer.errors import ErrorCode

from .conftest import log_case


def _headers(tenant, version):
    _, headers = tenant
    return {**headers, "x-api-version": version, "idempotency-key": "vk-1"}


def test_both_versions_served(client, tenant):
    for version in ("v1", "v2"):
        h = _headers(tenant, version)
        h["idempotency-key"] = f"vk-{version}"
        r = client.post("/api/resources", json={"name": "n"}, headers=h)
        log_case("version served", {"version": version}, "v1/v2 同时对外服务")
        assert r.status_code == 201, r.text


def test_unknown_version_rejected(client, tenant):
    r = client.post("/api/resources", json={"name": "n"}, headers=_headers(tenant, "v99"))
    log_case("unknown version", {"version": "v99"}, "不存在的版本必须 UNSUPPORTED_VERSION，不得静默降级")
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == ErrorCode.UNSUPPORTED_VERSION.value
    assert body["error"]["details"]["supported_versions"] == ["v1", "v2"]


def test_missing_version_rejected(client, tenant):
    _, headers = tenant
    headers = {k: v for k, v in headers.items() if k != "x-api-version"}
    r = client.post("/api/resources", json={"name": "n"}, headers=headers)
    log_case("missing version", {"x-api-version": None}, "未声明版本同样明确拒绝")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.UNSUPPORTED_VERSION.value


def test_v2_new_optional_field_accepted(client, tenant):
    h = _headers(tenant, "v2")
    r = client.post("/api/resources", json={"name": "n", "labels": {"env": "dev"}}, headers=h)
    log_case("compatible addition", {"version": "v2", "labels": {"env": "dev"}}, "v2 新增可选字段 labels 向后兼容")
    assert r.status_code == 201
    assert r.json()["labels"] == {"env": "dev"}


def test_v1_with_v2_field_is_incompatible_change(client, tenant):
    h = _headers(tenant, "v1")
    r = client.post("/api/resources", json={"name": "n", "labels": {}}, headers=h)
    log_case(
        "incompatible change",
        {"version": "v1", "field": "labels"},
        "v1 请求携带 v2 字段属于不兼容变更，必须 INCOMPATIBLE_CHANGE",
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == ErrorCode.INCOMPATIBLE_CHANGE.value


def test_unknown_field_rejected_not_ignored(client, tenant):
    h = _headers(tenant, "v2")
    r = client.post("/api/resources", json={"name": "n", "surprise": 1}, headers=h)
    log_case("unknown field", {"field": "surprise"}, "未知字段必须 VALIDATION_FAILED，不得静默忽略")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value


def test_missing_required_field(client, tenant):
    h = _headers(tenant, "v1")
    r = client.post("/api/resources", json={}, headers=h)
    log_case("missing required", {"payload": {}}, "缺必填字段 name 应 VALIDATION_FAILED")
    assert r.status_code == 400
    assert r.json()["error"]["details"]["missing"] == ["name"]


def test_extended_status_capability_gated_by_version(app, client, tenant):
    # 打开灰度开关后，v1 不支持 extended_status 能力，必须明确拒绝
    from integration_layer.feature_flags import FlagRule

    app.state.flags.replace({"extended-status": FlagRule(rollout=100)})
    h = _headers(tenant, "v1")
    r = client.post("/api/resources", json={"name": "n"}, headers=h)
    log_case(
        "capability gating",
        {"flag": "extended-status=on", "version": "v1"},
        "开关开启但 v1 无 extended_status 能力，必须 UNSUPPORTED_CAPABILITY",
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value

    h2 = _headers(tenant, "v2")
    r2 = client.post("/api/resources", json={"name": "n"}, headers=h2)
    assert r2.status_code == 201
    assert r2.json()["status_detail"] == "active:ok"
