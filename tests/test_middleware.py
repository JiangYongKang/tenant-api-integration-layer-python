"""ASGI 中间件专项：request_id 透传、分块流式超限拒绝、Content-Length 预检。"""

from __future__ import annotations

import asyncio
import json

from app.errors import make_error_body
from app.middleware import RequestLimitMiddleware
from app.stores import ConfigStore


class _DummyApp:
    def __init__(self):
        self.called = False
        self.received = b""

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            self.received += msg.get("body", b"")
        # 不应到达：超限请求由中间件直接响应
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


def _store(limit=100):
    from app.config import RateLimitRule, Settings

    s = Settings(
        config_version=1,
        tenants={},
        default_max_body_bytes=limit,
        default_rate_limit=RateLimitRule(1, 1),
    )
    return ConfigStore(s)


def _run(coro):
    return asyncio.run(coro)


def test_content_length_preflight_rejects_without_reading_body():
    """Content-Length 明确超限时立即 413，不消费 body、不调用下游。"""
    app = _DummyApp()
    mw = RequestLimitMiddleware(app, _store(limit=100))
    sent = []

    async def receive():
        raise AssertionError("预检拒绝时不应读取 body")

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": "POST", "path": "/api/v1/widgets",
        "headers": [(b"content-length", b"9999")],
    }
    _run(mw(scope, receive, send))
    assert not app.called
    start = sent[0]
    assert start["status"] == 413
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == "payload_too_large"
    assert body["error"]["details"]["detected_via"] == "content-length"
    assert body["error"]["request_id"]  # request_id 已贯穿


def test_chunked_streaming_oversize_rejects_while_reading():
    """无 Content-Length 的分块请求：累计字节超限后立即拒绝，剩余块不再读取。"""
    app = _DummyApp()
    mw = RequestLimitMiddleware(app, _store(limit=10))
    sent = []
    chunks = [b"x" * 8, b"y" * 8]  # 第二块使总数 16 > 10
    idx = {"i": 0}
    reads = {"n": 0}

    async def receive():
        reads["n"] += 1
        if idx["i"] < len(chunks):
            ch = chunks[idx["i"]]
            idx["i"] += 1
            return {"type": "http.request", "body": ch, "more_body": idx["i"] < len(chunks)}
        raise AssertionError("超限后不应继续读取后续块")

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": "POST", "path": "/x",
        "headers": [],
    }
    _run(mw(scope, receive, send))
    assert not app.called
    assert reads["n"] == 2  # 只读到触发超限为止，未无界拉取
    assert sent[0]["status"] == 413


def test_small_body_passes_through_cached_receive():
    """未超限的小请求体被完整缓存并交给下游，且带 request_id 响应头。"""
    app = _DummyApp()

    async def real_app(scope, receive, send):
        app.called = True
        msg = await receive()
        app.received = msg["body"]
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    mw = RequestLimitMiddleware(real_app, _store(limit=100))
    sent = []

    async def receive():
        return {"type": "http.request", "body": b'{"name":"x"}', "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    _run(mw(scope, receive, send))
    assert app.called and app.received == b'{"name":"x"}'
    headers = dict(sent[0]["headers"])
    assert b"x-request-id" in headers


def test_caller_supplied_request_id_is_honored():
    app = _DummyApp()

    async def real_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = RequestLimitMiddleware(real_app, _store())
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": "POST", "path": "/x",
        "headers": [(b"x-request-id", b"my-trace-42")],
    }
    _run(mw(scope, receive, send))
    headers = dict(sent[0]["headers"])
    assert headers[b"x-request-id"] == b"my-trace-42"


def test_illegal_caller_request_id_is_replaced():
    app = _DummyApp()

    async def real_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = RequestLimitMiddleware(real_app, _store())
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": "GET", "path": "/x",
        "headers": [(b"x-request-id", b"bad id with spaces!!" * 10)],
    }
    _run(mw(scope, receive, send))
    headers = dict(sent[0]["headers"])
    rid = headers[b"x-request-id"].decode()
    assert rid != "bad id" and len(rid) == 12
