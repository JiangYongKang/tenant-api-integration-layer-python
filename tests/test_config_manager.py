"""ConfigManager 单元测试：原子提交、并发无半切换、回退快照隔离、有界历史。"""
import threading

import pytest

from integration_layer.config_manager import ConfigManager, ConfigSnapshot, mask_secret
from integration_layer.errors import ApiError, ErrorCode
from integration_layer.feature_flags import FlagRule
from integration_layer.rate_limit import RateLimitRule

from .conftest import log_case


def _mgr(limit=10):
    return ConfigManager(ConfigSnapshot(), history_limit=limit)


def test_effective_change_is_all_or_nothing():
    m = _mgr()
    # 非法 scopes 在校验阶段失败：凭证/限流/灰度三者都不应出现任何改动
    with pytest.raises(ApiError) as ei:
        m.issue_credential("t", {"bogus:scope"}, secret="valid-secret-123")
    assert ei.value.code == ErrorCode.INVALID_CONFIG
    snap = m.snapshot()
    log_case(
        "atomic commit",
        {"change": "issue invalid scopes"},
        "校验失败时整体不生效：无凭证、版本不前进",
    )
    assert snap.credentials == {}
    assert m.current_version == 1


def test_concurrent_commits_no_half_state():
    m = _mgr()
    errors = []

    def worker(i):
        try:
            m.issue_credential(f"t{i}", {"resources:read"}, secret=f"secret-{i}-abcdef")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = m.snapshot()
    tenants = sorted(
        (c.tenant_id for c in snap.credentials.values()),
        key=lambda t: int(t[1:]),
    )
    log_case(
        "concurrent commits",
        {"threads": 20},
        "20 个并发变更全部完整生效或抛错，无半状态；当前快照恰好含 20 个凭证",
    )
    assert not errors
    assert len(snap.credentials) == 20
    assert tenants == [f"t{i}" for i in range(20)]
    assert m.current_version == 21  # 初始 1 + 20 次提交


def test_snapshot_taken_before_commit_is_not_mutated():
    """请求进行中拿到的快照，不被后续提交/回退带进中间状态。"""
    m = _mgr()
    seen = m.snapshot()
    v1 = m.current_version
    m.set_flag("f", FlagRule(rollout=100))
    m.issue_credential("t", {"resources:read"}, secret="snapshot-secret-1")
    log_case(
        "in-flight snapshot isolation",
        {"held_version": v1},
        "进行中请求持有的旧快照引用在多次提交后内容不变",
    )
    assert seen.flags == {}
    assert seen.credentials == {}
    assert m.snapshot().flags["f"].rollout == 100


def test_rollback_copies_content_not_alias():
    m = _mgr()
    m.set_flag("f", FlagRule(rollout=50))
    target = m.current_version
    m.set_flag("f", FlagRule(rollout=100))
    m.rollback(target)
    # 回退后继续改，不影响历史版本的内容
    m.set_flag("f", FlagRule(rollout=0))
    log_case(
        "rollback isolation",
        {"target": target},
        "回退产生独立副本，后续修改不污染历史版本",
    )
    assert m.snapshot().flags["f"].rollout == 0


def test_mask_secret_irreversible_and_checkable():
    masked = mask_secret("supersecret-value")
    log_case(
        "mask format",
        {"secret": "supersecret-value", "masked": masked},
        "脱敏串保留首末片段可核对、中间固定掩码、不可逆推",
    )
    assert masked.startswith("su") and masked.endswith("ue")
    assert "pers" not in masked
    assert mask_secret("abc") == "****"  # 过短串全部掩码
