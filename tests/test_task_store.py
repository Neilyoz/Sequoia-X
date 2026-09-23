"""tasks.db 存取层测试。"""

import sqlite3

from sequoia_x.runner.pipeline import StrategyOutcome
from sequoia_x.task.models import TaskKind, TaskRecord, TaskStatus
from sequoia_x.task.store import TaskStore


def _store(tmp_path) -> TaskStore:
    store = TaskStore(str(tmp_path / "tasks.db"))
    store.init_schema()
    return store


def _record(
    task_id: str = "t1",
    status: TaskStatus = TaskStatus.PENDING,
    kind: TaskKind = TaskKind.DAILY,
    created_at: str = "2026-09-23T10:00:00+08:00",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        kind=kind,
        status=status,
        triggered_by="test",
        params={"push": True},
        created_at=created_at,
    )


def test_init_schema_is_idempotent(tmp_path) -> None:
    """连续建表两次不报错：服务重启走同一条 init_schema 路径（全部 IF NOT EXISTS）。"""
    store = TaskStore(str(tmp_path / "tasks.db"))
    store.init_schema()
    store.init_schema()


def test_create_update_get_roundtrip_lossless(tmp_path) -> None:
    """字段经 JSON 序列化往返无损，含嵌套 result dict（T2 落库、T3 回读的地基）。"""
    store = _store(tmp_path)
    store.create(_record())
    loaded = store.get("t1")
    assert loaded is not None
    assert loaded.status is TaskStatus.PENDING
    assert loaded.params == {"push": True}
    assert loaded.result is None and loaded.error is None

    loaded.status = TaskStatus.SUCCESS
    loaded.result = {"outcomes": [{"n": 1}], "nested": {"deep": [1, 2]}}
    loaded.error = None
    loaded.finished_at = "2026-09-23T10:05:00+08:00"
    store.update(loaded)

    again = store.get("t1")
    assert again.status is TaskStatus.SUCCESS
    assert again.result == {"outcomes": [{"n": 1}], "nested": {"deep": [1, 2]}}
    assert again.finished_at == "2026-09-23T10:05:00+08:00"


def test_update_is_full_upsert(tmp_path) -> None:
    """update() 对不存在的 task_id 也生效（ON CONFLICT 单一路径）。"""
    store = _store(tmp_path)
    store.update(_record(task_id="ghost"))
    assert store.get("ghost") is not None


def test_get_missing_returns_none(tmp_path) -> None:
    assert _store(tmp_path).get("nope") is None


def test_active_finds_pending_then_none(tmp_path) -> None:
    """单飞兜底：有非终态任务返回它，全部终态后返回 None。"""
    store = _store(tmp_path)
    store.create(_record())
    assert store.active() is not None and store.active().task_id == "t1"
    rec = store.get("t1")
    rec.status = TaskStatus.SUCCESS
    store.update(rec)
    assert store.active() is None


def test_recover_interrupted_marks_failed(tmp_path) -> None:
    """脏 pending/running 一律改 failed 并占住终态；success 不受影响。

    finished_at 填恢复时刻是 store 层定的策略：凡终态必有完成时间（见其 docstring）。
    """
    store = _store(tmp_path)
    store.create(_record(task_id="dirty", status=TaskStatus.RUNNING))
    store.create(_record(task_id="clean", status=TaskStatus.SUCCESS))
    assert store.recover_interrupted() == 1
    dirty = store.get("dirty")
    assert dirty.status is TaskStatus.FAILED
    assert dirty.error == "进程重启导致任务中断"
    assert dirty.finished_at is not None
    assert store.get("clean").status is TaskStatus.SUCCESS


def test_save_signals_idempotent(tmp_path) -> None:
    """同 (task_id, strategy, symbol) 重复写不报错、不重复：靠 UNIQUE + OR IGNORE。"""
    store = _store(tmp_path)
    outcomes = [
        StrategyOutcome("StrategyA", "ma_volume", ["000001", "600519"], True),
        StrategyOutcome("StrategyB", "turtle", [], False),
    ]
    assert store.save_signals("t1", "2026-09-23", outcomes) == 2
    assert store.save_signals("t1", "2026-09-23", outcomes) == 0
    assert store.query_signals(trade_date="2026-09-23") != []


def test_prune_keeps_recent_and_cascades(tmp_path) -> None:
    """5 条任务 prune(keep=2)：只剩最近 2 条，其孤儿信号级联删除。"""
    store = _store(tmp_path)
    for i in range(1, 6):
        store.create(
            _record(
                task_id=f"t{i}",
                status=TaskStatus.SUCCESS,
                created_at=f"2026-09-{10 + i:02d}T10:00:00+08:00",
            )
        )
        store.save_signals(
            f"t{i}", f"2026-09-{10 + i:02d}", [StrategyOutcome("S", "turtle", ["600000"], False)]
        )
    store.prune(keep=2)
    ids = {r.task_id for r in store.list()[0]}
    assert ids == {"t5", "t4"}
    survivors = {row["task_id"] for row in store.query_signals()}
    assert survivors == {"t5", "t4"}


def test_prune_never_touches_non_task_tables(tmp_path) -> None:
    """prune 只删 task_run/signal：行情库是另一个文件，这里断言 tasks.db 里只有这两张表。"""
    store = _store(tmp_path)
    store.create(_record())
    store.prune(keep=0)

    with sqlite3.connect(store.db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"task_run", "signal", "sqlite_sequence"}


def test_query_signals_filters_and_paging(tmp_path) -> None:
    store = _store(tmp_path)
    outcomes_a = [StrategyOutcome("StrategyA", "ma_volume", ["000001", "600519"], True)]
    outcomes_b = [StrategyOutcome("StrategyB", "turtle", ["300750"], True)]
    store.save_signals("t1", "2026-09-22", outcomes_a)
    store.save_signals("t2", "2026-09-23", outcomes_b)

    by_date = store.query_signals(trade_date="2026-09-22")
    assert [s["symbol"] for s in by_date] == ["600519", "000001"]
    assert [s["strategy"] for s in store.query_signals(strategy="StrategyB")] == ["StrategyB"]
    assert [s["task_id"] for s in store.query_signals(symbol="300750")] == ["t2"]
    page1 = store.query_signals(limit=2, offset=0)
    page2 = store.query_signals(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 1
    assert {r["symbol"] for r in page1 + page2} == {"000001", "600519", "300750"}


def test_list_pagination_and_filters(tmp_path) -> None:
    store = _store(tmp_path)
    for i in range(1, 4):
        store.create(
            _record(
                task_id=f"t{i}",
                status=TaskStatus.SUCCESS if i < 3 else TaskStatus.FAILED,
                kind=TaskKind.DAILY if i < 3 else TaskKind.BACKFILL,
                created_at=f"2026-09-20T10:00:0{i}:00+08:00",
            )
        )
    records, total = store.list(status="success")
    assert total == 2 and {r.task_id for r in records} == {"t1", "t2"}
    records, total = store.list(kind="backfill", limit=10, offset=0)
    assert total == 1 and records[0].task_id == "t3"
    page, total = store.list(limit=2, offset=2)
    assert total == 3 and len(page) == 1


def test_append_log_tail_merges_into_result(tmp_path) -> None:
    """log_tail 写进 result_json，与既有 result 键共存不互相覆盖。"""
    store = _store(tmp_path)
    rec = _record()
    rec.result = {"synced_rows": 42}
    store.create(rec)
    store.append_log_tail("t1", ["line-1", "line-2"])
    loaded = store.get("t1")
    assert loaded.result == {"synced_rows": 42, "log_tail": ["line-1", "line-2"]}
    store.append_log_tail("t1", ["only"])
    assert store.get("t1").result["log_tail"] == ["only"]
