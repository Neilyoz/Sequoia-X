"""TaskManager 测试：单飞互斥（本任务核心）、提交立即返回、终态与信号落库、失败释放。

所有 pipeline 入口都被替换为假函数——绝不真连 baostock/飞书（红线）。
"""

import logging
import threading
import time

import pytest

from sequoia_x.core import logger as core_logger
from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.runner import pipeline
from sequoia_x.runner.pipeline import DailyReport, StrategyOutcome
from sequoia_x.task.logs import set_current_task
from sequoia_x.task.manager import TaskManager
from sequoia_x.task.models import TaskAlreadyRunning, TaskKind, TaskRecord, TaskStatus
from sequoia_x.task.store import TaskStore


def _detach(handler) -> None:
    if handler in core_logger._extra_handlers:
        core_logger._extra_handlers.remove(handler)
    for lg in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(lg, logging.Logger) and handler in lg.handlers:
            lg.removeHandler(handler)


def _report(trade_date: str = "2026-09-23") -> DailyReport:
    return DailyReport(
        trade_date=trade_date,
        synced_rows=2,
        outcomes=[
            StrategyOutcome("MaVolumeStrategy", "ma_volume", ["600519", "000001"], False)
        ],
        push_enabled=False,
    )


def _wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    """启动好的 TaskManager + store；两条 pipeline 入口先给安全默认假实现。"""
    settings = Settings(
        db_path=str(tmp_path / "quote.db"),  # 绝不允许指向真实行情库
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        task_db_path=str(tmp_path / "tasks.db"),
    )
    store = TaskStore(settings.task_db_path)
    monkeypatch.setattr(
        pipeline, "run_daily", lambda s, *, push=True, strategies=None: _report()
    )
    monkeypatch.setattr(pipeline, "run_backfill", lambda s: {"symbols_synced": 3})
    mgr = TaskManager(settings, store)
    mgr.start()
    try:
        yield mgr, store
    finally:
        mgr.shutdown(wait=True)
        _detach(mgr._capture)
        set_current_task(None)


def test_submit_returns_immediately_while_task_runs(manager, monkeypatch) -> None:
    """提交耗时 <0.5s 而假任务实跑 ≥1s：跑批在后台线程，HTTP 侧绝不陪着阻塞。"""
    mgr, store = manager
    start = time.monotonic()
    mgr.submit(TaskKind.DAILY, params={"push": False}, triggered_by="test")
    assert time.monotonic() - start < 0.5

    def slow(s, *, push=True, strategies=None):
        time.sleep(1.0)
        return _report()

    # 上面的提交已用默认假实现跑完，这里再提交一单慢任务验证"不等完成"
    _wait_until(lambda: mgr._current_task_id is None)
    monkeypatch.setattr(pipeline, "run_daily", slow)
    start = time.monotonic()
    slow_record = mgr.submit(TaskKind.DAILY, params={}, triggered_by="test")
    assert time.monotonic() - start < 0.5
    done = mgr.wait_for_completion(slow_record.task_id, timeout=10)
    assert time.monotonic() - start >= 1.0
    assert done.status is TaskStatus.SUCCESS


def test_single_flight_rejects_second_submit(manager, monkeypatch) -> None:
    """运行中再提交必须抛 TaskAlreadyRunning 且携带在跑任务 id——绝不排队。"""
    mgr, store = manager
    gate = threading.Event()

    def blocked(s, *, push=True, strategies=None):
        assert gate.wait(timeout=10), "假任务等待超时"
        return _report()

    monkeypatch.setattr(pipeline, "run_daily", blocked)
    first = mgr.submit(TaskKind.DAILY, params={}, triggered_by="test")
    assert _wait_until(lambda: store.get(first.task_id).status is TaskStatus.RUNNING)

    with pytest.raises(TaskAlreadyRunning) as ei:
        mgr.submit(TaskKind.BACKFILL, params={}, triggered_by="test2")
    assert ei.value.running_task_id == first.task_id
    assert ei.value.kind == TaskKind.DAILY
    active = mgr.active_task()
    assert active is not None and active.task_id == first.task_id

    gate.set()
    done = mgr.wait_for_completion(first.task_id, timeout=10)
    assert done.status is TaskStatus.SUCCESS
    assert mgr._current_task_id is None
    # 锁已归还：后续提交畅通
    second = mgr.submit(TaskKind.BACKFILL, params={}, triggered_by="test3")
    assert mgr.wait_for_completion(second.task_id, timeout=10).status is TaskStatus.SUCCESS


def test_ten_threads_submit_exactly_one_wins(manager, monkeypatch) -> None:
    """10 线程同刻撞锁：恰好 1 个成功、9 个被拒（约束 §3 锁临界区覆盖到入池）。"""
    mgr, store = manager
    gate = threading.Event()

    def blocked(s, *, push=True, strategies=None):
        assert gate.wait(timeout=10), "假任务等待超时"
        return _report()

    monkeypatch.setattr(pipeline, "run_daily", blocked)
    barrier = threading.Barrier(10)
    ok: list[TaskRecord] = []
    rejected: list[TaskAlreadyRunning] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=5)
        try:
            record = mgr.submit(TaskKind.DAILY, params={}, triggered_by="stress")
        except TaskAlreadyRunning as exc:
            with lock:
                rejected.append(exc)
        else:
            with lock:
                ok.append(record)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ok) == 1 and len(rejected) == 9
    assert {e.running_task_id for e in rejected} == {ok[0].task_id}
    gate.set()
    assert mgr.wait_for_completion(ok[0].task_id, timeout=10).status is TaskStatus.SUCCESS


def test_success_persists_result_signals_and_log_tail(manager, monkeypatch) -> None:
    """成功路径：result 含 outcomes、信号落库、log_tail 归集、单飞标志归还。"""

    def with_logs(s, *, push=True, strategies=None):
        get_logger("sequoia_x.test_mgr_fake_pipeline").info("fake-pipeline-log")
        return _report(trade_date="2026-09-21")

    monkeypatch.setattr(pipeline, "run_daily", with_logs)
    mgr, store = manager
    record = mgr.submit(
        TaskKind.DAILY,
        params={"push": True, "strategies": ["ma_volume"]},
        triggered_by="test",
    )
    done = mgr.wait_for_completion(record.task_id, timeout=10)

    assert done.status is TaskStatus.SUCCESS
    assert done.started_at and done.finished_at
    assert done.result["trade_date"] == "2026-09-21"
    assert len(done.result["outcomes"]) == 1
    assert any(line.endswith("fake-pipeline-log") for line in done.result["log_tail"])

    rows = store.query_signals(trade_date="2026-09-21")
    assert {r["symbol"] for r in rows} == {"600519", "000001"}
    assert {r["task_id"] for r in rows} == {record.task_id}
    assert mgr._current_task_id is None


def test_failure_releases_flag_and_allows_resubmit(manager, monkeypatch) -> None:
    """失败必须推进终态并归还单飞标志：否则一次异常把整个服务卡死。"""

    def boom(s, *, push=True, strategies=None):
        raise RuntimeError("模拟策略崩溃")

    monkeypatch.setattr(pipeline, "run_daily", boom)
    mgr, store = manager
    record = mgr.submit(TaskKind.DAILY, params={}, triggered_by="test")
    done = mgr.wait_for_completion(record.task_id, timeout=10)

    assert done.status is TaskStatus.FAILED
    assert "RuntimeError" in done.error and "模拟策略崩溃" in done.error
    assert done.finished_at is not None
    assert "log_tail" in done.result

    # 失败后还能提交（走 fixture 默认的假 run_backfill）
    second = mgr.submit(TaskKind.BACKFILL, params={}, triggered_by="test2")
    assert mgr.wait_for_completion(second.task_id, timeout=10).status is TaskStatus.SUCCESS


def test_backfill_task_stores_dict_result(manager) -> None:
    mgr, store = manager
    record = mgr.submit(TaskKind.BACKFILL, params={}, triggered_by="test")
    done = mgr.wait_for_completion(record.task_id, timeout=10)
    assert done.status is TaskStatus.SUCCESS
    assert done.result["symbols_synced"] == 3


def test_stale_active_row_is_adopted_and_rejected(manager) -> None:
    """内存标志为空但库里有非终态行（模拟重启丢失）：submit 拒绝并 adopt 该行 id。"""
    mgr, store = manager
    store.create(
        TaskRecord(
            task_id="stale-1",
            kind=TaskKind.DAILY,
            status=TaskStatus.PENDING,
            triggered_by="ghost",
        )
    )
    with pytest.raises(TaskAlreadyRunning) as ei:
        mgr.submit(TaskKind.DAILY, params={}, triggered_by="test")
    assert ei.value.running_task_id == "stale-1"
    assert mgr._current_task_id == "stale-1"
    # 交还干净，不影响 fixture 收尾
    mgr._current_task_id = None


def test_submit_before_start_raises_and_wait_rejects_unknown(tmp_path) -> None:
    """未 start 就提交属于编程错误（RuntimeError）；wait_for_completion 对未知 id 抛 KeyError。"""
    settings = Settings(
        db_path=str(tmp_path / "quote.db"),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        task_db_path=str(tmp_path / "tasks.db"),
    )
    store = TaskStore(settings.task_db_path)
    store.init_schema()  # 只建表不 start()：验证未启动路径
    mgr = TaskManager(settings, store)
    with pytest.raises(RuntimeError):
        mgr.submit(TaskKind.DAILY, params={}, triggered_by="test")
    with pytest.raises(KeyError):
        mgr.wait_for_completion("not-exist", timeout=0.1)
