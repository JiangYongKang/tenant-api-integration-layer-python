"""进程入口：构建本地默认配置并启动 ASGI 应用。"""

from __future__ import annotations

import time

from app.bootstrap import build_default_settings
from app.dependencies import build_deps
from app.logging_setup import configure_logging
from app.main import create_app

logger = configure_logging()
_settings, _tokens = build_default_settings(int(time.time()))
deps = build_deps(_settings, logger=logger)
app = create_app(deps)
