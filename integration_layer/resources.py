"""本地资源存储：幂等写入的副作用落点，线程安全，状态只在内存。"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Resource:
    id: str
    tenant_id: str
    name: str
    status: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], Resource] = {}

    def create(self, tenant_id: str, name: str, status: str = "active") -> Resource:
        res = Resource(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            name=name,
            status=status,
            created_at=time.time(),
        )
        with self._lock:
            self._items[(tenant_id, res.id)] = res
        return res

    def get(self, tenant_id: str, resource_id: str) -> Resource | None:
        with self._lock:
            return self._items.get((tenant_id, resource_id))

    def count(self, tenant_id: str) -> int:
        with self._lock:
            return sum(1 for (t, _) in self._items if t == tenant_id)
