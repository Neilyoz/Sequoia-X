"""调度层测试：装配/降级/单飞三条主线（任务书 T4 §5），不依赖真实时间流逝。

红线：全程不触网——pipeline 入口一律 monkeypatch 假实现，行情库/任务库都指向
tmp_path，绝不写真 data/ 下任何文件。§5 清单最后一条"单飞集成"是本任务最重要的
测试：守住"调度与手动并发时不会双跑 baostock"（约束 §3.3）。
"""

import asyncio
import logging
import threading
import time
import zoneinfo
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from sequoia_x.core import logger as core_logger
from sequoia_x.core.config import Settings
from sequoia_x.runner import pipeline
from sequoia_x.runner.pipeline import DailyReport, StrategyOutcome
from sequoia_x.scheduler.jobs import _daily_job, _resolve_timezone, build_scheduler
from sequoia_x.task.logs import set_current_task
from sequoia_x.task.manager import TaskManager
from sequoia_x.task.models import TaskAlreadyRunning, TaskKind, TaskStatus
from sequoia_x.task.store import TaskStore


def _settings(tmp_path, **overrides) -> Settings:
    """测试专用 Settings：全部落 tmp_path，_env_file=None 隔离真实 .env。"""
    kwargs = dict(
        _env_file=None,
        db_path=str(tmp_path / "quote.db"),  # 绝不允许指向真实行情库
        feishu_webhook_url="https://example.com/hook",
        task_db_path=str(tmp_path / "tasks.db"),
        scheduler_enabled=True,
        schedule_cron="15 19 * * 1-5",
        timezone="Asia/Shanghai",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _detach(handler) -> None:
    """摘掉 TaskLogCapture handler（与 test_task_manager 同法，防跨测试泄漏）。"""
    if handler in core_logger._extra_handlers:
        core_logger._extra_handlers.remove(handler)
    for lg in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(lg, logging.Logger) and handler in lg.handlers:
            lg.removeHandler(handler)


def _report() -> DailyReport:
    return DailyReport(
        trade_date="2026-09-23",
        synced_rows=2,
        outcomes=[StrategyOutcome("MaVolumeStrategy", "ma_volume", ["600519"], False)],
        push_enabled=False,
    )


def _wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def caplog_jobs(caplog):
    """让 sequoia_x.scheduler.jobs 的日志冒泡给 caplog。

    core/logger 的业务 logger 全部 propagate=False（rich 直挂自家 handler），caplog
    只挂在 root 上收不到；测试期间临时打开传播，不改 core/logger 本身行为。
    """
    import sequoia_x.scheduler.jobs as jobs_module

    caplog.set_level(logging.INFO)
    jobs_module.logger.propagate = True
    yield caplog
    jobs_module.logger.propagate = False


# ── §5.1/§5.2 装配降级 ──


def test_build_scheduler_returns_none_when_disabled(tmp_path) -> None:
    """scheduler_enabled=false → 不装调度器，返回 None（约束 §6：默认必须关）。"""
    settings = _settings(tmp_path, scheduler_enabled=False)
    assert build_scheduler(settings, MagicMock()) is None


def test_build_scheduler_returns_none_on_invalid_cron(tmp_path, caplog_jobs) -> None:
    """cron 写错只记 ERROR 并返回 None——服务照常启动，查询接口不能被一个字符串拖挂。"""
    settings = _settings(tmp_path, schedule_cron="不是cron")
    manager = MagicMock()
    assert build_scheduler(settings, manager) is None  # 关键：没抛异常
    assert any("非法" in r.message and r.levelno == logging.ERROR for r in caplog_jobs.records)


# ── §5.3 合法 cron 装配断言 ──


def test_build_scheduler_registers_daily_job_with_valid_cron(tmp_path) -> None:
    """合法 cron：job 存在、触发时分=19:15、单飞/补跑参数与任务书 §4.2 一致。

    AsyncIOScheduler.start() 要求运行中的事件循环，且 get_job 只在 RUNNING 态可用，
    所以装配断言整个放进一个临时 loop 里完成。
    """
    settings = _settings(tmp_path)
    scheduler = build_scheduler(settings, MagicMock())
    assert isinstance(scheduler, AsyncIOScheduler)

    async def scenario() -> None:
        scheduler.start()
        try:
            job = scheduler.get_job("daily_pipeline")
            assert job is not None
            # 固定假时间避开"下一个工作日"跨天断言的脆弱性：周三上午 10 点 → 今晚 19:15
            now = datetime(2026, 9, 23, 10, 0, tzinfo=_resolve_timezone("Asia/Shanghai"))
            nxt = job.trigger.get_next_fire_time(None, now)
            assert (nxt.hour, nxt.minute) == (19, 15)
            assert job.misfire_grace_time == 300
            assert job.coalesce is True
            assert job.max_instances == 1
        finally:
            scheduler.shutdown(wait=False)

    asyncio.run(scenario())


def test_build_scheduler_timezone_falls_back_to_utc8(tmp_path, monkeypatch) -> None:
    """缺 IANA tz 库时回退固定 UTC+8（与 models._get_tz 同策略），调度器仍能装配。"""
    import sequoia_x.scheduler.jobs as jobs_module

    def _boom(_name):
        raise LookupError("模拟 Windows 精简发行版无 tzdata")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", _boom)
    tz = jobs_module._resolve_timezone("Asia/Shanghai")
    assert tz.utcoffset(None).total_seconds() == 8 * 3600
    settings = _settings(tmp_path)
    assert build_scheduler(settings, MagicMock()) is not None


# ── §5.4/§5.5 _daily_job 单元行为 ──


def test_daily_job_submits_via_manager_with_scheduler_trigger(tmp_path) -> None:
    """_daily_job 只走 manager.submit：kind=DAILY、params 带 push、triggered_by=scheduler。"""
    settings = _settings(tmp_path)
    manager = MagicMock()
    manager.submit.return_value = MagicMock(task_id="fake-task-id")
    _daily_job(settings, manager)
    manager.submit.assert_called_once_with(
        TaskKind.DAILY, params={"push": True}, triggered_by="scheduler"
    )


def test_daily_job_swallows_task_already_running(tmp_path, caplog_jobs) -> None:
    """单飞冲突不外冒（调度线程不能被搞崩），且记中文 WARNING 供排查。"""
    settings = _settings(tmp_path)
    manager = MagicMock()
    manager.submit.side_effect = TaskAlreadyRunning("busy-task-1", TaskKind.DAILY)
    _daily_job(settings, manager)  # 不抛异常即通过
    assert any(
        "定时跑批被跳过，已有任务在跑：busy-task-1" in r.message
        and r.levelno == logging.WARNING
        for r in caplog_jobs.records
    )


def test_daily_job_swallows_unexpected_submit_error(tmp_path, caplog_jobs) -> None:
    """兜底：submit 抛任何异常都不外冒（比如 manager 未启动的 RuntimeError）。"""
    settings = _settings(tmp_path)
    manager = MagicMock()
    manager.submit.side_effect = RuntimeError("TaskManager 未启动")
    _daily_job(settings, manager)
    assert any("定时跑批提交失败" in r.message for r in caplog_jobs.records)


# ── §5.6 端到端：DateTrigger(now) 真起调度器 → 任务落库（代替被红线否决的等 cron 联调） ──


def test_e2e_scheduler_job_persists_task_with_scheduler_trigger(tmp_path, monkeypatch) -> None:
    """手工 DateTrigger 触发一次：tasks.db 出现 triggered_by="scheduler" 的终态记录。

    时区一致性（验收 §6.4）一并断言：created_at 带 +08:00 偏移，与调度器时区同源。
    """
    monkeypatch.setattr(pipeline, "run_daily", lambda s, **kw: _report())
    settings = _settings(tmp_path)
    store = TaskStore(settings.task_db_path)
    manager = TaskManager(settings, store)
    manager.start()

    async def scenario() -> str | None:
        scheduler = AsyncIOScheduler(timezone=_resolve_timezone(settings.timezone))
        scheduler.start()
        try:
            tz = _resolve_timezone(settings.timezone)
            scheduler.add_job(
                _daily_job,
                trigger=DateTrigger(run_date=datetime.now(tz)),
                args=[settings, manager],
                id="manual_e2e",
            )
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                records, total = store.list(limit=10)
                if records and records[0].status.is_terminal:
                    return records[0].task_id
                await asyncio.sleep(0.05)
            return None
        finally:
            scheduler.shutdown(wait=False)

    try:
        task_id = asyncio.run(scenario())
        assert task_id is not None, "DateTrigger 触发后任务未在 5s 内落库并到终态"
        record = store.get(task_id)
        assert record.triggered_by == "scheduler"
        assert record.kind == TaskKind.DAILY.value
        assert record.status is TaskStatus.SUCCESS
        assert record.created_at.endswith("+08:00")  # 与 task_run.created_at 时区同源
    finally:
        manager.shutdown(wait=True)
        _detach(manager._capture)
        set_current_task(None)


# ── §5.7 单飞集成（本任务最重要的测试） ──


def test_daily_job_skipped_while_manual_task_running(tmp_path, monkeypatch, caplog_jobs) -> None:
    """DAILY 手动任务在跑时触发 _daily_job：只产生 1 条记录，第 2 次被跳过并记 WARNING。

    守住的性质：调度与手动并发时不会双跑 baostock（约束 §3.3 单飞锁对两条入口
    同等生效——这正是 _daily_job 必须走 submit 而不是直调 pipeline 的理由）。
    """
    gate = threading.Event()

    def blocked(s, **kw):
        assert gate.wait(timeout=10), "假任务等待超时"
        return _report()

    monkeypatch.setattr(pipeline, "run_daily", blocked)
    settings = _settings(tmp_path)
    store = TaskStore(settings.task_db_path)
    manager = TaskManager(settings, store)
    manager.start()
    try:
        first = manager.submit(TaskKind.DAILY, params={"push": True}, triggered_by="api")
        assert _wait_until(lambda: store.get(first.task_id).status is TaskStatus.RUNNING)

        _daily_job(settings, manager)  # 模拟 cron 到点：绝不许抛、绝不许排队

        records, total = store.list(limit=10)
        assert total == 1, f"单飞被绕过：库里有 {total} 条记录"
        assert store.get(first.task_id).triggered_by == "api"  # 没被调度器改写
        assert any(
            "定时跑批被跳过，已有任务在跑" in r.message and r.levelno == logging.WARNING
            for r in caplog_jobs.records
        )

        gate.set()
        done = manager.wait_for_completion(first.task_id, timeout=10)
        assert done.status is TaskStatus.SUCCESS
        records, total = store.list(limit=10)
        assert total == 1  # 跳过的调度不留下任何排队残留
    finally:
        manager.shutdown(wait=True)
        _detach(manager._capture)
        set_current_task(None)
