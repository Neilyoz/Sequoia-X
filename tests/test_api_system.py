"""T3 系统路由测试：/health、/api/info、/api/strategies（T3 §6）。

全部用 Settings(_env_file=None, ...) 显式注入假配置：不吃仓库根 .env、
不碰真实行情库（db_path 指向 tmp）、不触任何网络（约束 §9 离线要求）。
"""

import logging
import os

import pytest
from fastapi.testclient import TestClient

from sequoia_x.api.app import create_app
from sequoia_x.core.config import Settings
from sequoia_x.runner.registry import STRATEGY_CLASSES

API_KEY = "sekret"
AUTH = {"X-API-Key": API_KEY}


def _detach_task_log_captures() -> None:
    """摘除本文件 lifespan 挂上的 TaskLogCapture handler。

    T2 的 TaskManager.start() 只挂不摘，core.logger.register_handler 的注册表
    又是进程级的；API 测试每个 app 实例都会留一个常驻 capture，累计后会让
    test_logger.py 属性 3（新建 logger 只应有 1 个 handler）假红。测试侧自行
    收尾，不改 T2 代码。
    """
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
    """每条测试结束都清扫一次，杜绝跨文件污染。"""
    yield
    _detach_task_log_captures()


@pytest.fixture
def clean_webhook_env(monkeypatch):
    """抹掉 STRATEGY_WEBHOOK_* 环境变量。

    import sequoia_x.api.app 时 bootstrap() 已把真实 .env 载入 os.environ，
    而 Settings.model_post_init 会扫描 os.environ 里的 STRATEGY_WEBHOOK_ 前缀——
    不清掉的话 has_dedicated_webhook 断言就依赖开发机的 .env，换台机器即红。
    """
    for key in list(os.environ):
        if key.upper().startswith("STRATEGY_WEBHOOK_"):
            monkeypatch.delenv(key, raising=False)


def build_client(tmp_path, **overrides) -> TestClient:
    """构造带假 Settings 的 TestClient（含 lifespan 启停），db_path 指向 tmp 空库。"""
    settings = Settings(
        _env_file=None,
        feishu_webhook_url="http://feishu.invalid/hook",
        db_path=str(tmp_path / "market_stub.db"),
        task_db_path=str(tmp_path / "tasks.db"),
        api_key=API_KEY,
        **overrides,
    )
    return TestClient(create_app(settings))


def test_health_ok_without_key(tmp_path) -> None:
    """守住的性质：/health 是探针，不挂鉴权依赖（架构 §4 鉴权列"否"）。"""
    with build_client(tmp_path) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_strategies_list_matches_registry_without_dedicated_webhook(
    tmp_path, clean_webhook_env
) -> None:
    """守住的性质：/api/strategies 输出注册表全量 7 条、顺序一致、不实例化策略；
    只配了默认 FEISHU_WEBHOOK_URL 时 has_dedicated_webhook 全为 false（T3 §6）。"""
    with build_client(tmp_path) as client:
        resp = client.get("/api/strategies", headers=AUTH)
    assert resp.status_code == 200
    items = resp.json()
    assert [i["name"] for i in items] == [cls.__name__ for cls in STRATEGY_CLASSES]
    assert {i["webhook_key"] for i in items} == {cls.webhook_key for cls in STRATEGY_CLASSES}
    assert all(i["has_dedicated_webhook"] is False for i in items)


def test_strategies_reflects_dedicated_webhook_env(
    tmp_path, clean_webhook_env, monkeypatch
) -> None:
    """守住的性质：STRATEGY_WEBHOOK_MA_VOLUME 配了才报 true——帮用户确认 .env 配对没配对。"""
    monkeypatch.setenv("STRATEGY_WEBHOOK_MA_VOLUME", "http://feishu.invalid/ma-volume")
    with build_client(tmp_path) as client:
        items = client.get("/api/strategies", headers=AUTH).json()
    flags = {i["webhook_key"]: i["has_dedicated_webhook"] for i in items}
    assert flags["ma_volume"] is True
    assert sum(flags.values()) == 1


def test_info_fields_complete_and_configurable(tmp_path) -> None:
    """守住的性质：/api/info 是前端/运维的自述卡，字段集合是对外契约（T3 §4.5）。"""
    with build_client(tmp_path) as client:
        resp = client.get("/api/info", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "name",
        "version",
        "timezone",
        "scheduler_enabled",
        "schedule_cron",
        "strategy_count",
        "active_task_id",
        "db_path",
        "started_at",
    }
    assert body["version"] == "3.0.0"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["scheduler_enabled"] is False
    assert body["schedule_cron"] == "15 19 * * 1-5"
    assert body["strategy_count"] == len(STRATEGY_CLASSES)
    assert body["active_task_id"] is None
    assert body["started_at"]
