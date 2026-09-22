"""灰度开关：请求内取值一致、原子切换、非法配置拒绝/回退、分桶确定。"""
import pytest

from integration_layer.errors import ApiError, ErrorCode
from integration_layer.feature_flags import FlagRule, FlagStore

from .conftest import log_case


def test_snapshot_consistent_within_request():
    store = FlagStore({"f": FlagRule(rollout=50)})
    snap = store.snapshot()
    before = [snap.is_enabled("f", "tenant-x") for _ in range(5)]
    store.replace({"f": FlagRule(rollout=100 if not before[0] else 0)})
    after = [snap.is_enabled("f", "tenant-x") for _ in range(5)]
    log_case(
        "snapshot consistency",
        {"rollout": "50 -> flipped mid-request"},
        "同一请求持有同一快照，运行中配置变更不影响本次取值",
    )
    assert all(v == before[0] for v in before + after)


def test_replace_is_atomic_no_half_switched_state():
    store = FlagStore({"a": FlagRule(rollout=100), "b": FlagRule(rollout=0)})
    store.replace({"a": FlagRule(rollout=0), "b": FlagRule(rollout=100)})
    snap = store.snapshot()
    log_case(
        "atomic replace",
        {"a": "100->0", "b": "0->100"},
        "replace 整体生效：a 与 b 必须同时切换，不存在中间态",
    )
    assert snap.is_enabled("a", "t") is False
    assert snap.is_enabled("b", "t") is True


def test_invalid_config_rejected_and_old_kept():
    store = FlagStore({"f": FlagRule(rollout=100)})
    with pytest.raises(ApiError) as ei:
        store.replace({"f": FlagRule(rollout=150)})
    log_case(
        "invalid config rejected",
        {"rollout": 150},
        "非法 rollout 应抛 INVALID_CONFIG 且保留旧配置",
    )
    assert ei.value.code == ErrorCode.INVALID_CONFIG
    assert store.snapshot().is_enabled("f", "t") is True


def test_invalid_override_type_rejected():
    with pytest.raises(ApiError) as ei:
        FlagRule(rollout=10, overrides={"t": "yes"})
    log_case("invalid override", {"override": "yes"}, "override 非布尔应被拒绝")
    assert ei.value.code == ErrorCode.INVALID_CONFIG


def test_rollout_bucket_deterministic_per_tenant():
    rule = FlagRule(rollout=50)
    snap_store = FlagStore({"f": rule})
    snap = snap_store.snapshot()
    samples = {t: snap.is_enabled("f", t) for t in (f"tenant-{i}" for i in range(200))}
    log_case(
        "deterministic rollout",
        {"rollout": 50, "tenants": 200},
        "同一租户多次判定一致；整体命中率应接近 50%（允许误差）",
    )
    for t, v in samples.items():
        assert snap.is_enabled("f", t) == v
    hits = sum(samples.values())
    assert 60 < hits < 140


def test_override_beats_rollout_and_unknown_flag_defaults_off():
    store = FlagStore({"f": FlagRule(rollout=0, overrides={"vip": True})})
    snap = store.snapshot()
    log_case(
        "override & safe default",
        {"rollout": 0, "override": {"vip": True}},
        "租户级 override 优先；未配置开关安全默认关闭",
    )
    assert snap.is_enabled("f", "vip") is True
    assert snap.is_enabled("f", "other") is False
    assert snap.is_enabled("never-configured", "vip") is False
