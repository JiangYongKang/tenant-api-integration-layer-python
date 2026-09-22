"""组合根：集中持有全部本地状态与依赖，供路由与测试复用。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from logging import Logger
from typing import Callable

from app.config import Settings
from app.logging_setup import configure_logging
from app.stores import ConfigStore, IdempotencyStore, RateLimitStore, ResourceStore

# 可注入的时间源（毫秒），默认真实时钟；测试可替换为可控时钟
ClockMs = Callable[[], int]


@dataclass
class Deps:
    config_store: ConfigStore
    idempotency: IdempotencyStore
    rate_limits: RateLimitStore
    resources: ResourceStore
    clock_ms: ClockMs
    logger: Logger

    def snapshot_settings(self) -> Settings:
        return self.config_store.current()


def default_clock_ms() -> int:
    return int(time.time() * 1000)


def build_deps(
    settings: Settings,
    clock_ms: ClockMs | None = None,
    logger: Logger | None = None,
) -> Deps:
    """构造默认依赖（全部进程内状态）。"""
    return Deps(
        config_store=ConfigStore(settings),
        idempotency=IdempotencyStore(),
        rate_limits=RateLimitStore(),
        resources=ResourceStore(),
        clock_ms=clock_ms or default_clock_ms,
        logger=logger or configure_logging(),
    )
