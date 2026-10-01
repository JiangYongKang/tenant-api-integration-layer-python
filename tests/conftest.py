import logging
import sys

import pytest
from fastapi.testclient import TestClient

from integration_layer.app import LayerConfig, create_app

# 用例日志：打印输入与判定依据
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
case_log = logging.getLogger("testcase")


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def tenant(app):
    """注册一个具备读写权限的租户，返回 (tenant_id, headers)。"""
    key_id, secret = app.state.credentials.register(
        "tenant-a", {"resources:read", "resources:write"}
    )
    case_log.info("fixture: registered tenant-a key_id=%s", key_id)
    return "tenant-a", {
        "x-api-key-id": key_id,
        "x-api-secret": secret,
        "x-api-version": "v1",
    }


@pytest.fixture()
def v2_tenant(app):
    """注册 v2（具备资源生命周期能力）的读写租户，返回请求头。"""
    key_id, secret = app.state.credentials.register(
        "tenant-life", {"resources:read", "resources:write"}
    )
    return {
        "x-api-key-id": key_id,
        "x-api-secret": secret,
        "x-api-version": "v2",
    }


def log_case(title: str, inputs: dict, basis: str) -> None:
    case_log.info("CASE %s | inputs=%s | basis=%s", title, inputs, basis)
