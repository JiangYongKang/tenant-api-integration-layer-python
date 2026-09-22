"""进程内状态存储（本地、线程安全）。

- 配置存储：原子快照 + 读写锁，替换走整体替换；
- 幂等记录：每键一把锁，保证同一幂等键并发请求串行裁决且结果收敛；
- 限流计数：固定窗口计数，按 (租户, 接口) 隔离；
- 资源存储：写入受幂等键保护，重复提交只产生一次副作用。

状态仅保存在内存中（本地约束），进程重启即清空。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import Settings


@dataclass
class IdempotencyRecord:
    tenant_id: str
    key: str
    request_hash: str
    status_code: int
    response_body: dict[str, Any]
    completed: bool = False


@dataclass
class RateCounter:
    window_started_ms: int
    count: int = 0


class ConfigStore:
    """保存当前生效的不可变配置快照。

    ``replace`` 先校验、后整体赋值（引用替换在 Python 下原子），
    进行中的请求持有旧快照引用，天然看不到半切换状态。
    """

    def __init__(self, initial: Settings) -> None:
        self._snapshot = initial
        self._lock = threading.RLock()

    def current(self) -> Settings:
        with self._lock:
            return self._snapshot

    def replace(self, next_settings: Settings) -> int:
        """原子替换，返回新版本号。调用方必须已完成 next_settings 的校验。"""
        with self._lock:
            self._snapshot = next_settings
            return next_settings.config_version


class IdempotencyStore:
    """幂等记录与每键互斥锁。

    每键锁保证：同一 (租户, 幂等键) 的并发请求被串行裁决，
    第二个请求必然看到第一个请求写入的结果，从而收敛一致。
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, tenant_id: str, key: str) -> threading.Lock:
        """获取（或创建）某幂等键的专用锁。"""
        k = (tenant_id, key)
        with self._guard:
            lock = self._key_locks.get(k)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[k] = lock
            return lock

    def get(self, tenant_id: str, key: str) -> IdempotencyRecord | None:
        with self._guard:
            return self._records.get((tenant_id, key))

    def put(self, record: IdempotencyRecord) -> None:
        with self._guard:
            self._records[(record.tenant_id, record.key)] = record


class RateLimitStore:
    """按 (租户, 接口) 隔离的固定窗口计数器。

    不同租户、不同接口使用不同计数器与不同锁：
    突发流量既不串扰，也不会互相阻塞。
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, str], RateCounter] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, k: tuple[str, str]) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(k)
            if lock is None:
                lock = threading.Lock()
                self._locks[k] = lock
            return lock

    def hit(
        self,
        tenant_id: str,
        endpoint_key: str,
        now_ms: int,
        limit: int,
        window_ms: int,
    ) -> tuple[bool, int, int]:
        """记一次请求，返回 (是否放行, 当前计数, 窗口重置剩余毫秒)。

        超限时不增加计数（保持计数=窗口内被接受的请求数），
        并返回剩余等待毫秒，便于调用方给出稳定的重试提示。
        """
        k = (tenant_id, endpoint_key)
        with self._lock_for(k):
            counter = self._counters.get(k)
            if counter is None or now_ms - counter.window_started_ms >= window_ms:
                counter = RateCounter(window_started_ms=now_ms, count=0)
                self._counters[k] = counter
            reset_in_ms = window_ms - (now_ms - counter.window_started_ms)
            if counter.count >= limit:
                return False, counter.count, max(reset_in_ms, 1)
            counter.count += 1
            return True, counter.count, max(reset_in_ms, 1)


class ResourceStore:
    """写入产生的业务资源；副作用次数可观测（供测试断言）。"""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._guard = threading.Lock()
        self.side_effect_count = 0

    def create(self, item: dict[str, Any]) -> dict[str, Any]:
        """真正产生副作用的创建（每个幂等键只允许成功执行一次）。"""
        with self._guard:
            item_id = uuid.uuid4().hex
            stored = dict(item)
            stored["id"] = item_id
            self._items[item_id] = stored
            self.side_effect_count += 1
            return stored

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self._guard:
            stored = self._items.get(item_id)
            return None if stored is None else dict(stored)

    def list_all(self) -> list[dict[str, Any]]:
        with self._guard:
            return [dict(v) for v in self._items.values()]
