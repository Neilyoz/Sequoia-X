"""T3 API Key 鉴权测试（T3 §6）。

鉴权语义（架构 §4 / 约束 §7）：API_KEY 为空 = 全部免鉴权（开发语义）；
配置后缺失与错误统一 401，响应体不区分两种失败、不回显 key 的任何片段。
"""

import logging

import pytest
from fastapi.testclient import TestClient

from sequoia_x.api.app import create_app
from sequoia_x.core.config import Settings

API_KEY = "sekret"
PROTECTED_PATHS = ["/api/info", "/api/strategies", "/api/tasks"]


def _detach_task_log_captures() -> None:
    """摘除 lifespan 挂上的 TaskLogCapture：T2 的挂载是进程级的且只挂不摘，
    不清扫会让 test_logger.py 属性 3（新建 logger 只应有 1 个 handler）假红。"""
    from sequoia_x.core import logger as core_logger
    from sequoia_x.task.logs import TaskLogCapture

    core_logger._extra_handlers[:] = [
        h for h in core_logger._extra_handlers if not isinstance(h, TaskLogCapture)
    ]
    for lg in logging.Logger.manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            lg.handlers[:] = [h for h in lg.handlers if not isinstance(h, TaskLogCapture)]


@pytest.fixture(autouse=True)
def clean_task_log_captures():
    yield
    _detach_task_log_captures()


def build_client(tmp_path, api_key: str) -> TestClient:
    settings = Settings(
        _env_file=None,
        feishu_webhook_url="http://feishu.invalid/hook",
        db_path=str(tmp_path / "market_stub.db"),
        task_db_path=str(tmp_path / "tasks.db"),
        api_key=api_key,
    )
    return TestClient(create_app(settings))


@pytest.fixture
def client(tmp_path):
    with build_client(tmp_path, API_KEY) as c:
        yield c


def test_protected_endpoints_without_key_return_401(client) -> None:
    """守住的性质：router 级依赖声明覆盖所有受保护路径，漏挂即安全洞（T3 §4.3）。"""
    for path in PROTECTED_PATHS:
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["detail"] == {"code": "unauthorized"}, path


def test_post_endpoint_without_key_returns_401_before_body_check(client) -> None:
    """守住的性质：写接口在 body 校验与提交之前先 401，未鉴权请求零副作用。"""
    resp = client.post("/api/tasks/daily", json={})
    assert resp.status_code == 401
    listing = client.get("/api/tasks", headers={"X-API-Key": API_KEY}).json()
    assert listing["total"] == 0


def test_wrong_key_and_missing_key_indistinguishable(client) -> None:
    """守住的性质：401 不区分"key 不存在"与"key 不对"（架构 §4），
    且响应体不回显 key 的任何片段（不泄露凭据信息）。"""
    missing = client.get("/api/info")
    wrong = client.get("/api/info", headers={"X-API-Key": "wr0ng-key"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()
    assert "sekret" not in missing.text
    assert "ekret" not in wrong.text


def test_health_exempt_from_auth(client) -> None:
    """守住的性质：/health 免鉴权（探针/负载均衡可用，架构 §4）。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_correct_key_grants_access(client) -> None:
    """守住的性质：X-API-Key 正确时受保护接口正常放行。"""
    headers = {"X-API-Key": API_KEY}
    assert client.get("/api/info", headers=headers).status_code == 200
    assert client.get("/api/strategies", headers=headers).status_code == 200
    assert client.get("/api/tasks", headers=headers).status_code == 200


def test_empty_api_key_means_open_mode(tmp_path) -> None:
    """守住的性质：API_KEY 未配置时全部接口免鉴权可用（约束 §7 的开发语义）。"""
    with build_client(tmp_path, "") as client:
        assert client.get("/api/info").status_code == 200
        assert client.get("/api/tasks").status_code == 200
        # 带一个随机 key 也不该被拒（open 模式不检查头）
        assert client.get("/api/info", headers={"X-API-Key": "whatever"}).status_code == 200
