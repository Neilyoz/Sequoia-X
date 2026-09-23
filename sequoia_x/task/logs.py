"""按 task_id 归集日志。

坑（动手前已实测，T2 §4.4）：core/logger.py 给每个业务模块 logger 设了
propagate = False，把捕获 handler 挂在 "sequoia_x" 父 logger 上**收不到任何
子 logger 的日志**。正确挂载点是"逐个已存在的 sequoia_x.* logger 添加 handler +
通过 logger.register_handler 钩子覆盖未来新建的 logger"，见 attach_to_sequoia_loggers。
"""

import logging
from collections import deque
from contextvars import ContextVar
from datetime import datetime

_current_task_id: ContextVar[str | None] = ContextVar("sequoia_x_current_task_id", default=None)


def set_current_task(task_id: str | None) -> None:
    """绑定/解绑当前执行任务。ContextVar 自带线程隔离，无需额外加锁。"""
    _current_task_id.set(task_id)


def get_current_task() -> str | None:
    return _current_task_id.get()


class TaskLogCapture(logging.Handler):
    """把当前任务的日志行缓冲到内存环形队列，任务结束时 drain 出来落库。

    纯文本行、不带 rich 着色（要进 SQLite）。无 task_id 上下文（CLI 直调
    pipeline）时静默丢弃，不报错——CLI 场景本来就没有归集需求。
    并发安全依赖 logging.Handler.handle 自带的 per-handler 锁。
    """

    def __init__(self, tail_lines: int = 200) -> None:
        super().__init__()
        self._tail_lines = tail_lines
        self._buffers: dict[str, deque[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        task_id = get_current_task()
        if task_id is None:
            return
        stamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        self._buffers.setdefault(task_id, deque(maxlen=self._tail_lines)).append(
            f"{stamp} {record.levelname} {record.getMessage()}"
        )

    def get_lines(self, task_id: str) -> list[str]:
        """查看（不清空）某任务当前缓冲。"""
        return list(self._buffers.get(task_id, ()))

    def drain(self, task_id: str) -> list[str]:
        """取出并移除该任务的缓冲——每个任务只在结束时被取走一次，不留内存垃圾。"""
        return list(self._buffers.pop(task_id, ()))


def attach_to_sequoia_loggers(handler: logging.Handler) -> None:
    """把 handler 挂到所有已存在与今后新建的 sequoia_x.* logger 上，幂等可重复调用。"""
    from sequoia_x.core import logger as core_logger

    core_logger.register_handler(handler)
    for name, lg in logging.Logger.manager.loggerDict.items():
        if not isinstance(lg, logging.Logger):
            continue
        if (name == "sequoia_x" or name.startswith("sequoia_x.")) and handler not in lg.handlers:
            lg.addHandler(handler)


def detach_from_sequoia_loggers(handler: logging.Handler) -> None:
    """attach_to_sequoia_loggers 的逆操作：从注册表与所有已挂 logger 上摘除。

    没有这一步，每个新建的 TaskManager 都会往进程级 logger 树上漏一个 handler：
    测试里多个 app 实例叠挂会让日志缓冲跨任务串台（T3 实测踩到，
    多个 TestClient app 让存量 test_logger 属性测试假红）。
    """
    from sequoia_x.core import logger as core_logger

    if handler in core_logger._extra_handlers:
        core_logger._extra_handlers.remove(handler)
    for lg in logging.Logger.manager.loggerDict.values():
        if isinstance(lg, logging.Logger) and handler in lg.handlers:
            lg.removeHandler(handler)
