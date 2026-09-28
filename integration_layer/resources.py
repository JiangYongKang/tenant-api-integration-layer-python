"""本地资源存储：创建 / 更新 / 删除，乐观并发控制，墓碑保证删除幂等。

并发语义：
- 每个资源带单调递增的整数 version；更新必须携带期望 version。
- 两个请求拿着同一旧 version 并发更新时，恰好一个成功；另一个得到
  CONCURRENT_CONFLICT，并在 details 中给出服务端当前 version，调用方可据此
  判断自己手上的状态已过期（与参数错误 / 鉴权 / 幂等冲突明确区分）。
- 删除保留墓碑（tombstone）：重复删除返回一致结果，不变成内部错误或残留。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from .errors import ApiError, ErrorCode


@dataclass
class Resource:
    id: str
    tenant_id: str
    name: str
    status: str
    created_at: float
    version: int = 1
    updated_at: float | None = None
    labels: dict[str, Any] = field(default_factory=dict)
    deleted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], Resource] = {}

    def create(
        self,
        tenant_id: str,
        name: str,
        status: str = "active",
        labels: dict[str, Any] | None = None,
    ) -> Resource:
        res = Resource(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            name=name,
            status=status,
            created_at=time.time(),
            labels=dict(labels or {}),
        )
        with self._lock:
            self._items[(tenant_id, res.id)] = res
        return res

    def get(self, tenant_id: str, resource_id: str) -> Resource | None:
        """返回活动资源；已删除（墓碑）对外等同不存在。"""
        with self._lock:
            res = self._items.get((tenant_id, resource_id))
            if res is None or res.deleted:
                return None
            return res

    def _get_live_locked(self, tenant_id: str, resource_id: str) -> Resource | None:
        res = self._items.get((tenant_id, resource_id))
        if res is None or res.deleted:
            return None
        return res

    def update(
        self,
        tenant_id: str,
        resource_id: str,
        expected_version: int,
        changes: dict[str, Any],
    ) -> Resource:
        """乐观并发更新。version 过期 -> CONCURRENT_CONFLICT，附带服务端当前版本。"""
        with self._lock:
            res = self._get_live_locked(tenant_id, resource_id)
            if res is None:
                raise ApiError(ErrorCode.NOT_FOUND, "resource not found")
            if expected_version != res.version:
                raise ApiError(
                    ErrorCode.CONCURRENT_CONFLICT,
                    "resource was modified by another request; refetch and retry",
                    details={
                        "expected_version": expected_version,
                        "current_version": res.version,
                    },
                )
            if "name" in changes:
                res.name = changes["name"]
            if "status" in changes:
                res.status = changes["status"]
            if "labels" in changes:
                res.labels = dict(changes["labels"])
            res.version += 1
            res.updated_at = time.time()
            return res

    def delete(
        self, tenant_id: str, resource_id: str, expected_version: int | None = None
    ) -> Resource:
        """删除资源（置墓碑）。删除本身幂等：重复删除返回一致的墓碑视图。

        携带 expected_version 时仍做乐观并发校验；version 过期 -> CONCURRENT_CONFLICT。
        """
        with self._lock:
            res = self._items.get((tenant_id, resource_id))
            if res is not None and res.deleted:
                # 已删：直接返回保留的墓碑本身，跨幂等键也给出字段一致的结果
                return res
            if res is None:
                # 从未存在过：给出稳定、可解释的合成墓碑
                return Resource(
                    id=resource_id,
                    tenant_id=tenant_id,
                    name="",
                    status="deleted",
                    created_at=0.0,
                    version=0,
                    deleted=True,
                )
            if expected_version is not None and expected_version != res.version:
                raise ApiError(
                    ErrorCode.CONCURRENT_CONFLICT,
                    "resource was modified by another request; refetch and retry",
                    details={
                        "expected_version": expected_version,
                        "current_version": res.version,
                    },
                )
            res.deleted = True
            res.status = "deleted"
            res.version += 1
            res.updated_at = time.time()
            return res

    def count(self, tenant_id: str) -> int:
        with self._lock:
            return sum(1 for (t, _) in self._items if t == tenant_id)
