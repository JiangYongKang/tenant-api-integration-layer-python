"""幂等写入：按 (tenant, 操作类型, idempotency_key) 收敛结果，副作用只执行一次。

语义：
- 相同键 + 相同操作 + 相同内容指纹 -> 返回首次结果（replay），不重复执行副作用。
- 相同键 + 相同操作 + 不同内容 -> IDEMPOTENCY_CONFLICT。
- 同一幂等键用于不同操作时各算各的（操作类型是键的一部分），互不串扰。
- 并发同键请求通过 per-key 锁收敛：后到者等待首个完成并重放其结果。
- 处理中状态若因异常退出会被清理，不留残留。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ApiError, ErrorCode

# 操作类型：不同操作的幂等命名空间互相隔离
OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"


def content_fingerprint(payload: dict[str, Any]) -> str:
    """请求内容的规范指纹（键序无关）。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _Record:
    fingerprint: str
    result: Any = None
    done: bool = False


class IdempotencyStore:
    """线程安全的本地幂等记录库。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str, str], _Record] = {}
        self._key_locks: dict[tuple[str, str, str], threading.Lock] = {}

    def _key_lock(self, key: tuple[str, str, str]) -> threading.Lock:
        with self._lock:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._key_locks[key] = lk
            return lk

    def execute(
        self,
        tenant_id: str,
        idem_key: str,
        fingerprint: str,
        side_effect: Callable[[], Any],
        op: str = OP_CREATE,
    ) -> tuple[Any, bool]:
        """执行幂等写入，返回 (result, replayed)。

        result 为 side_effect 的首次返回值（重放时逐字返回，包括状态码与响应体）。
        """
        key = (tenant_id, op, idem_key)
        with self._key_lock(key):
            rec = self._records.get(key)
            if rec is not None:
                if rec.fingerprint != fingerprint:
                    raise ApiError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key reused with different payload",
                        details={"operation": op},
                    )
                return rec.result, True
            rec = _Record(fingerprint=fingerprint)
            self._records[key] = rec
            try:
                rec.result = side_effect()
                rec.done = True
            except BaseException:
                # 失败不留残留记录，允许调用方安全重试
                with self._lock:
                    self._records.pop(key, None)
                raise
            return rec.result, False
