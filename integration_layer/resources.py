"""本地资源存储：幂等写入的副作用落点，线程安全，状态只在内存。"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class Resource:
    id: str
    tenant_id: str
    name: str
    status: str
    created_at: float
    # 生命周期扩展字段：带默认值，不改变既有构造/序列化行为
    version: int = 1
    labels: dict[str, Any] | None = None
    deleted: bool = False
    deleted_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """既有视图：创建/GET 响应保持原样，不含生命周期新字段。"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
        }

    def lifecycle_dict(self) -> dict[str, Any]:
        """生命周期视图：供更新/删除接口使用，含并发版本与删除标记。"""
        out = self.as_dict()
        out["version"] = self.version
        if self.labels is not None:
            out["labels"] = self.labels
        if self.deleted:
            out["deleted"] = True
            out["deleted_at"] = self.deleted_at
        return out


class ResourceStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], Resource] = {}

    def create(
        self, tenant_id: str, name: str, status: str = "active",
        labels: dict[str, Any] | None = None,
    ) -> Resource:
        res = Resource(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            name=name,
            status=status,
            created_at=time.time(),
            labels=dict(labels) if labels is not None else None,
        )
        with self._lock:
            self._items[(tenant_id, res.id)] = res
        return res

    def get(self, tenant_id: str, resource_id: str) -> Resource | None:
        """既有语义：只返回存活资源，tombstone 对外不可见。"""
        with self._lock:
            res = self._items.get((tenant_id, resource_id))
            if res is None or res.deleted:
                return None
            return res

    def count(self, tenant_id: str) -> int:
        with self._lock:
            return sum(
                1
                for (t, _), res in self._items.items()
                if t == tenant_id and not res.deleted
            )

    # ---- 生命周期：更新（乐观并发）与删除（幂等） ----

    def compare_and_update(
        self,
        tenant_id: str,
        resource_id: str,
        expected_version: int,
        changes: dict[str, Any],
    ) -> tuple[str, Resource | None]:
        """基于版本号的 CAS 更新。

        返回 (outcome, resource)：
        - ("ok", resource)：更新成功，版本号 +1；
        - ("stale", resource)：expected_version 过期，resource 携带当前版本；
        - ("not_found", None)：资源不存在或已删除。
        检查与写入在同一把锁内完成，并发同版本更新恰好一个成功。
        """
        with self._lock:
            res = self._items.get((tenant_id, resource_id))
            if res is None or res.deleted:
                return "not_found", None
            if res.version != expected_version:
                return "stale", res
            if "name" in changes:
                res.name = changes["name"]
            if "labels" in changes:
                res.labels = dict(changes["labels"])
            res.version += 1
            return "ok", res

    def delete(self, tenant_id: str, resource_id: str) -> tuple[str, Resource | None]:
        """幂等删除。

        返回 (outcome, resource)：
        - ("deleted", resource)：本次完成删除；
        - ("already_deleted", resource)：此前已删除，返回同一份 tombstone 视图；
        - ("not_found", None)：资源从未存在。
        """
        with self._lock:
            res = self._items.get((tenant_id, resource_id))
            if res is None:
                return "not_found", None
            if res.deleted:
                return "already_deleted", res
            res.deleted = True
            res.deleted_at = time.time()
            res.version += 1
            return "deleted", res
