"""任务日志归集测试：上下文互不串台、无上下文静默、环形截断、挂载点覆盖。"""

import logging
import threading

import pytest

from sequoia_x.core import logger as core_logger
from sequoia_x.task.logs import (
    TaskLogCapture,
    attach_to_sequoia_loggers,
    detach_from_sequoia_loggers,
    set_current_task,
)


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("sequoia_x.test", logging.INFO, "fake.py", 1, msg, None, None)


def _detach(handler: logging.Handler) -> None:
    """清扫：走生产的 detach，顺带验证它真的可用、不留残留。"""
    detach_from_sequoia_loggers(handler)


@pytest.fixture(autouse=True)
def _clean_task_context():
    """ContextVar 在主线程跨测试存续，必须每条测试后归零。"""
    yield
    set_current_task(None)


def test_concurrent_task_contexts_do_not_crosstalk() -> None:
    """两个任务上下文并发写日志，各自缓冲区只收自己的行（ContextVar 线程隔离）。"""
    capture = TaskLogCapture(tail_lines=50)
    try:
        def worker(task_id: str) -> None:
            set_current_task(task_id)
            for i in range(5):
                capture.handle(_record(f"{task_id}-{i}"))

        threads = [threading.Thread(target=worker, args=(f"task-{k}",)) for k in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for k in range(2):
            lines = capture.get_lines(f"task-{k}")
            assert {line.split()[-1] for line in lines} == {f"task-{k}-{i}" for i in range(5)}
    finally:
        capture.close()


def test_emit_without_task_context_is_silent() -> None:
    """CLI 直调 pipeline 没有任务上下文：不报错、也不产生任何缓冲。"""
    capture = TaskLogCapture()
    set_current_task(None)
    capture.handle(_record("孤儿日志"))
    assert capture._buffers == {}


def test_tail_lines_cap_keeps_latest(tmp_path) -> None:
    """deque 截断在源头生效：上限 3 时写 5 条只剩最新 3 条。"""
    capture = TaskLogCapture(tail_lines=3)
    set_current_task("t-cap")
    for i in range(5):
        capture.handle(_record(f"line-{i}"))
    lines = capture.get_lines("t-cap")
    assert [line.split()[-1] for line in lines] == ["line-2", "line-3", "line-4"]
    # 行格式为 "时间 LEVEL 消息"，纯文本可落库
    assert all(line.startswith("20") and " INFO " in line for line in lines)


def test_drain_pops_buffer() -> None:
    capture = TaskLogCapture()
    set_current_task("t-drain")
    capture.handle(_record("hello"))
    assert capture.get_lines("t-drain") != []
    drained = capture.drain("t-drain")
    assert len(drained) == 1 and drained[0].endswith("hello")
    assert capture._buffers == {}
    assert capture.drain("t-drain") == []  # 重复 drain 幂等返回空


def test_attach_covers_existing_and_future_loggers() -> None:
    """已建 logger 靠遍历补挂，之后新建的靠 register_handler 钩子——两条路都要通。

    这里验证的就是 logs.py 模块 docstring 记的坑：叶子 logger propagate=False，
    只挂 "sequoia_x" 父 logger 是收不到任何日志的。
    """
    capture = TaskLogCapture()
    existing = core_logger.get_logger("sequoia_x.test_existing_logger")
    try:
        attach_to_sequoia_loggers(capture)
        future = core_logger.get_logger("sequoia_x.test_future_logger")

        assert capture in existing.handlers
        assert capture in future.handlers

        set_current_task("t-attach")
        existing.info("hello-existing")
        future.info("hello-future")
        lines = capture.get_lines("t-attach")
        assert any(line.endswith("hello-existing") for line in lines)
        assert any(line.endswith("hello-future") for line in lines)

        # 幂等：重复 attach 不重复挂 handler
        before = list(future.handlers)
        attach_to_sequoia_loggers(capture)
        assert list(future.handlers) == before
    finally:
        _detach(capture)
        capture.close()


def test_detach_cleans_up_everywhere() -> None:
    """detach 是 attach 的完整逆操作：注册表、已挂 logger、emit 三条路径都摘干净。"""
    capture = TaskLogCapture()
    try:
        attached = core_logger.get_logger("sequoia_x.test_detach_logger")
        attach_to_sequoia_loggers(capture)
        assert capture in attached.handlers
        assert capture in core_logger._extra_handlers

        detach_from_sequoia_loggers(capture)
        assert capture not in attached.handlers
        assert capture not in core_logger._extra_handlers
        # detach 之后新建的 logger 不再被钩子挂上
        fresh = core_logger.get_logger("sequoia_x.test_detach_fresh")
        assert capture not in fresh.handlers
        set_current_task("t-detach")
        attached.info("摘除后的日志不应再进缓冲")
        assert capture._buffers == {}
    finally:
        _detach(capture)
        capture.close()
