"""T3 任务触发/查询路由测试（T3 §6）。

run_daily / run_backfill 全部 patch 掉（manager._execute 是按 pipeline 模块属性调用的，
patch 模块属性即生效）：绝不真跑批、绝不真碰 baostock/akshare/飞书（红线）。
TestClient 是同步的但 TaskManager 用真线程池，fixture 收尾必须等 active_task()
归 None 再走 lifespan 关闭，否则未 join 的非守护线程会拖红后续测试文件。
"""

import logging
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sequoia_x.api.app import create_app
from sequoia_x.core.config import Settings
from sequoia_x.runner.pipeline import DailyReport, StrategyOutcome

API_KEY = "sekret"
AUTH = {"X-API-Key": API_KEY}

RUN_DAILY = "sequoia_x.runner.pipeline.run_daily"
RUN_BACKFILL = "sequoia_x.runner.pipeline.run_backfill"


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


def fake_report() -> DailyReport:
    """构造与真实 pipeline 同形态的 DailyReport，供 manager 落 result/signals。"""
    return DailyReport(
        trade_date="2026-09-23",
        synced_rows=3,
        outcomes=[
            StrategyOutcome(
                strategy_name="MaVolumeStrategy",
                webhook_key="ma_volume",
                symbols=["600000"],
                pushed=True,
            )
        ],
        push_enabled=True,
    )


@pytest.fixture
def client_and_app(tmp_path):
    """起真 lifespan 的 TestClient；db_path 指向 tmp，测试全程不碰真实行情库。"""
    settings = Settings(
        _env_file=None,
        feishu_webhook_url="http://feishu.invalid/hook",
        db_path=str(tmp_path / "market_stub.db"),
        task_db_path=str(tmp_path / "tasks.db"),
        api_key=API_KEY,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app
        manager = app.state.manager
        deadline = time.monotonic() + 30
        while manager.active_task() is not None and time.monotonic() < deadline:
            time.sleep(0.05)


def test_post_daily_returns_202_and_persists_success(client_and_app) -> None:
    """守住的性质：触发接口 1 秒内返回 202 且不等待跑批（T3 §7.3），
    成功任务落库带 result 与 log_tail（架构 §3.6）。"""
    client, app = client_and_app
    with patch(RUN_DAILY, return_value=fake_report()) as mock_run:
        resp = client.post("/api/tasks/daily", json={}, headers=AUTH)
        assert resp.status_code == 202
        body = resp.json()
        assert body["task_id"]
        assert body["status"] in ("pending", "running")
        assert body["kind"] == "daily"
        assert body["triggered_by"] == "api"
        assert body["params"] == {"push": True, "strategies": None}
        record = app.state.manager.wait_for_completion(body["task_id"], timeout=15)
    assert record.status.value == "success"
    assert record.result["trade_date"] == "2026-09-23"
    assert isinstance(record.result["log_tail"], list)
    mock_run.assert_called_once()


def test_push_false_and_strategies_forwarded_to_pipeline(client_and_app) -> None:
    """守住的性质：body 参数原样透传给 run_daily 的关键字参数（约束 §6 的只跑不推语义）。"""
    client, app = client_and_app
    with patch(RUN_DAILY, return_value=fake_report()) as mock_run:
        resp = client.post(
            "/api/tasks/daily",
            json={"push": False, "strategies": ["ma_volume"]},
            headers=AUTH,
        )
        task_id = resp.json()["task_id"]
        app.state.manager.wait_for_completion(task_id, timeout=15)
    kwargs = mock_run.call_args.kwargs
    assert kwargs["push"] is False
    assert kwargs["strategies"] == ["ma_volume"]


def test_second_submit_while_running_returns_409(client_and_app) -> None:
    """守住的性质：单飞冲突拒绝并返回 409 且不排队（约束 §3），
    detail 里的 task_id/kind 指向正在跑的那个任务（架构 §4）。"""
    client, app = client_and_app
    started = threading.Event()
    release = threading.Event()

    def blocking_run_daily(settings, *, push=True, strategies=None):
        started.set()
        assert release.wait(timeout=20)
        return fake_report()

    with patch(RUN_DAILY, side_effect=blocking_run_daily):
        first = client.post("/api/tasks/daily", json={}, headers=AUTH).json()
        assert started.wait(10), "后台任务未启动，线程池或 patch 没生效"
        try:
            conflict = client.post("/api/tasks/daily", json={}, headers=AUTH)
        finally:
            release.set()
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["code"] == "task_already_running"
    assert detail["task_id"] == first["task_id"]
    assert detail["kind"] == "daily"
    app.state.manager.wait_for_completion(first["task_id"], timeout=15)


def test_unknown_strategy_rejected_400_before_submit(client_and_app) -> None:
    """守住的性质：策略名提交前预检，未知名 → 400 且带 allowed 列表（T3 §4.5），
    不占用单飞锁、不触达 pipeline。"""
    client, app = client_and_app
    with patch(RUN_DAILY, return_value=fake_report()) as mock_run:
        resp = client.post("/api/tasks/daily", json={"strategies": ["瞎写"]}, headers=AUTH)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "unknown_strategy"
    assert "MaVolumeStrategy" in detail["allowed"]
    assert "ma_volume" in detail["allowed"]
    mock_run.assert_not_called()
    assert app.state.manager.active_task() is None


def test_post_backfill_returns_202(client_and_app) -> None:
    """守住的性质：回填同样异步提交，202 + kind=backfill；run_backfill 被 patch 不外呼。"""
    client, app = client_and_app
    with patch(RUN_BACKFILL, return_value={"symbols_synced": 10}) as mock_backfill:
        resp = client.post("/api/tasks/backfill", headers=AUTH)
        assert resp.status_code == 202
        body = resp.json()
        assert body["kind"] == "backfill"
        assert body["params"] == {}
        record = app.state.manager.wait_for_completion(body["task_id"], timeout=15)
    assert record.status.value == "success"
    assert record.result["symbols_synced"] == 10
    mock_backfill.assert_called_once()


def test_list_pagination_total_and_filters(client_and_app) -> None:
    """守住的性质：列表按新→旧分页、total 为过滤后总数，kind 过滤生效（T3 §6）。"""
    client, app = client_and_app
    with patch(RUN_DAILY, return_value=fake_report()):
        for _ in range(3):
            task_id = client.post("/api/tasks/daily", json={}, headers=AUTH).json()["task_id"]
            app.state.manager.wait_for_completion(task_id, timeout=15)

    page = client.get("/api/tasks", params={"limit": 2}, headers=AUTH).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["limit"] == 2 and page["offset"] == 0
    assert all(i["kind"] == "daily" for i in page["items"])

    rest = client.get("/api/tasks", params={"limit": 2, "offset": 2}, headers=AUTH).json()
    assert rest["total"] == 3 and len(rest["items"]) == 1

    empty = client.get("/api/tasks", params={"kind": "backfill"}, headers=AUTH).json()
    assert empty["total"] == 0 and empty["items"] == []


def test_list_rejects_invalid_enum_and_range(client_and_app) -> None:
    """守住的性质：非法 status/kind 由 FastAPI 枚举校验自动 422；limit 上限 500（T3 §4.5）。"""
    client, _ = client_and_app
    assert client.get("/api/tasks", params={"status": "foo"}, headers=AUTH).status_code == 422
    assert client.get("/api/tasks", params={"kind": "foo"}, headers=AUTH).status_code == 422
    assert client.get("/api/tasks", params={"limit": 501}, headers=AUTH).status_code == 422
    assert client.get("/api/tasks", params={"offset": -1}, headers=AUTH).status_code == 422


def test_get_missing_task_returns_404(client_and_app) -> None:
    """守住的性质：任务不存在 → 404 + task_not_found 结构（架构 §4）。"""
    client, _ = client_and_app
    resp = client.get("/api/tasks/0000deadbeef", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"code": "task_not_found", "task_id": "0000deadbeef"}
