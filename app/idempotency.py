"""幂等写入裁决。

规则：
- 以 (租户, 幂等键) 命名空间，跨租户键互不可见、不复用；
- 键+相同内容的重复请求返回首次的状态码与响应体，不再产生副作用；
- 键相同但内容哈希不同 -> 409 idempotency_conflict；
- 并发同键请求在每键锁内串行裁决，结果收敛一致。

半成品（in-flight）记录策略：首个请求执行前先落一个 completed=False 的
占位记录；若它执行中失败（抛异常），占位记录被清除并释放锁，后续请求
可重试，避免"首次失败后键永久卡死"，也不会留下状态残留。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.errors import APIError, ErrorCode
from app.stores import IdempotencyRecord, IdempotencyStore

IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_KEY_LEN = 200


@dataclass(frozen=True)
class IdempotentResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool  # True 表示这是重复请求返回的缓存结果


def validate_key(key: str) -> str:
    """校验幂等键形态：非空、可见 ASCII、长度有上限。"""
    if not key:
        raise APIError(
            ErrorCode.VALIDATION_FAILED,
            422,
            f"幂等键不能为空（请求头 {IDEMPOTENCY_HEADER}）",
            details={"header": IDEMPOTENCY_HEADER},
        )
    if len(key) > MAX_KEY_LEN:
        raise APIError(
            ErrorCode.VALIDATION_FAILED,
            422,
            f"幂等键长度超过上限 {MAX_KEY_LEN} 字符",
            details={"header": IDEMPOTENCY_HEADER, "max_length": MAX_KEY_LEN},
        )
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in key):
        raise APIError(
            ErrorCode.VALIDATION_FAILED,
            422,
            "幂等键只能包含可见 ASCII 字符",
            details={"header": IDEMPOTENCY_HEADER},
        )
    return key


def content_hash(method: str, path: str, body: bytes) -> str:
    """对请求方法+路径+原始字节做稳定哈希（JSON 键序差异也能识别）。"""
    h = hashlib.sha256()
    h.update(method.encode("ascii"))
    h.update(b"\n")
    h.update(path.encode("utf-8"))
    h.update(b"\n")
    h.update(body)
    return h.hexdigest()


def enforce_tenant_body_limit(body: bytes, tenant_limit: int, global_limit: int) -> None:
    """认证完成后的租户级请求体上限二次校验。

    ASGI 层已用全局硬上限截断读取，这里仅对已读字节做长度比较，
    不产生额外内存拷贝；超出租户上限返回稳定的 413。
    """
    limit = min(tenant_limit, global_limit)
    if len(body) > limit:
        raise APIError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            413,
            f"请求体超过该租户允许的上限 {limit} 字节，已拒绝",
            details={
                "max_body_bytes": limit,
                "observed_bytes": len(body),
                "detected_via": "tenant_limit",
            },
        )


def execute_idempotent(
    store: IdempotencyStore,
    tenant_id: str,
    key: str,
    method: str,
    path: str,
    body: bytes,
    producer: Callable[[], tuple[int, dict[str, Any]]],
) -> IdempotentResult:
    """在幂等保护下执行 producer；producer 对同一键至多成功执行一次。

    返回结果带 ``replayed`` 标记。键相同但内容不同直接冲突拒绝。
    """
    validate_key(key)
    req_hash = content_hash(method, path, body)
    lock = store.lock_for(tenant_id, key)
    with lock:
        existing = store.get(tenant_id, key)
        if existing is not None:
            if existing.request_hash != req_hash:
                raise APIError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    409,
                    "幂等键已被用于不同的请求内容，拒绝执行以避免歧义；"
                    "请为新请求使用新的幂等键",
                    details={
                        "idempotency_key": key,
                        "reason": "request_content_mismatch",
                    },
                )
            if not existing.completed:
                # 理论上持锁后不会出现（占位记录只在锁内存在短暂窗口），
                # 兜底翻译为并发冲突，要求调用方重试。
                raise APIError(
                    ErrorCode.CONCURRENT_CONFLICT,
                    409,
                    "同一幂等键的前序请求尚未完成，请稍后重试",
                    details={"idempotency_key": key},
                )
            return IdempotentResult(existing.status_code, existing.response_body, True)

        placeholder = IdempotencyRecord(
            tenant_id=tenant_id,
            key=key,
            request_hash=req_hash,
            status_code=0,
            response_body={},
            completed=False,
        )
        store.put(placeholder)
        try:
            status_code, response_body = producer()
        except BaseException:
            # 执行失败：清除占位，允许调用方用同键重试，避免状态残留
            # 与"键卡死"。锁随 with 退出释放，无资源泄漏。
            with store._guard:  # noqa: SLF001 - 同模块受控访问
                store._records.pop((tenant_id, key), None)
            raise
        store.put(
            IdempotencyRecord(
                tenant_id=tenant_id,
                key=key,
                request_hash=req_hash,
                status_code=status_code,
                response_body=response_body,
                completed=True,
            )
        )
        return IdempotentResult(status_code, response_body, False)
