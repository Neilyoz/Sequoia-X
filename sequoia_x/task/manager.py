"""TaskManager：全局单飞互斥 + 单线程线程池，把 runner.pipeline 的同步长任务丢进后台。

约束 §3 是本文件存在的理由：baostock 用进程级全局 socket，同进程任意时刻只允许
一个跑批在动。因此——
- 线程池 max_workers 固定为 1（参数保留是为了测试注入）；
- 已有 pending/running 任务时新提交直接抛 TaskAlreadyRunning（→409），**不排队**：
  排队会让用户重复点击堆积出一串注定互相踩连接的失败；
- 内存标志为主、store.active() 兜底进程重启后丢失的标志。
整个包不引入事件循环框架（架构 §3.7）：这些操作都是微秒级 SQLite 读，T3 路由直接调用。
"""

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.runner import pipeline
from sequoia_x.task.logs import TaskLogCapture, attach_to_sequoia_loggers, set_current_task
from sequoia_x.task.models import (
    TaskAlreadyRunning,
    TaskKind,
    TaskRecord,
    TaskStatus,
    _now_iso,
)
from sequoia_x.task.store import TaskStore


class TaskManager:
    """提交/查询后台任务的唯一入口。生命周期（start/shutdown）由上层（T3 lifespan）管。"""

    def __init__(self, settings: Settings, store: TaskStore, *, max_workers: int = 1) -> None:
        # max_workers 生产固定 1（约束 §3 单飞），参数化只为测试可注入。
        self._settings = settings
        self._store = store
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._current_task_id: str | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._capture = TaskLogCapture(tail_lines=settings.task_log_tail_lines)
        self._logger = get_logger(__name__)

    def start(self) -> None:
        """建表、恢复脏任务、裁剪历史、挂日志捕获 handler、起线程池。"""
        self._store.init_schema()
        recovered = self._store.recover_interrupted()
        if recovered:
            self._logger.warning(f"启动恢复：{recovered} 条中断任务已标记为失败")
        self._store.prune(self._settings.task_history_limit)
        attach_to_sequoia_loggers(self._capture)
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="task"
        )

    def shutdown(self, wait: bool = False) -> None:
        """关池不杀任务：正在跑的 baostock 连接被硬切会留下脏 socket，比等它跑完更糟。"""
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=False)
            self._executor = None

    def submit(
        self, kind: TaskKind, *, params: dict[str, Any], triggered_by: str
    ) -> TaskRecord:
        """建 PENDING 记录并提交线程池，立即返回，不等待执行。

        锁临界区必须一路覆盖到 executor.submit：两个并发请求若都在入池前
        通过 active 检查，就会同时排队两个跑批（约束 §3 的竞态窗口）。
        """
        with self._lock:
            if self._executor is None:
                raise RuntimeError("TaskManager 未启动（需先调用 start()）")
            if self._current_task_id is not None:
                running = self._store.get(self._current_task_id)
                raise TaskAlreadyRunning(
                    self._current_task_id, running.kind if running else kind
                )
            stale = self._store.active()
            if stale is not None:
                # 兜底重启后丢失的内存标志；adopt 进内存，让后续冲突报同一个 id
                self._current_task_id = stale.task_id
                raise TaskAlreadyRunning(stale.task_id, TaskKind(stale.kind))
            record = TaskRecord(
                task_id=uuid.uuid4().hex,
                kind=kind,
                status=TaskStatus.PENDING,
                triggered_by=triggered_by,
                params=dict(params),
                created_at=_now_iso(),
            )
            self._store.create(record)
            self._current_task_id = record.task_id
            self._executor.submit(self._execute, record.task_id, kind, dict(params))
        return record

    def _execute(self, task_id: str, kind: TaskKind, params: dict[str, Any]) -> None:
        """工作线程执行体：无论成败必须推进到终态并归还单飞标志，否则锁被永久占死。"""
        set_current_task(task_id)
        record = self._store.get(task_id)
        assert record is not None  # 记录在 submit 锁内刚写入，同库同进程不可能丢
        record.status = TaskStatus.RUNNING
        record.started_at = _now_iso()
        self._store.update(record)
        try:
            if kind is TaskKind.DAILY:
                report = pipeline.run_daily(
                    self._settings,
                    push=params.get("push", True),
                    strategies=params.get("strategies"),
                )
                record.result = report.to_dict()
                self._store.save_signals(task_id, report.trade_date, report.outcomes)
            else:
                record.result = pipeline.run_backfill(self._settings)
            record.status = TaskStatus.SUCCESS
        except BaseException as exc:
            # 抓 BaseException：线程里的 SystemExit/KeyboardInterrupt 同样必须推进
            # 终态，Exception 之外的漏网异常会让单飞永久卡死（架构 §3.7）。
            record.status = TaskStatus.FAILED
            tb_tail = "".join(traceback.format_exception(exc))
            record.error = f"{type(exc).__name__}: {exc}\n{tb_tail[-2000:]}"
            self._logger.exception(f"任务 {task_id}（{kind.value}）执行失败")
        finally:
            record.finished_at = _now_iso()
            record.result = {**(record.result or {}), "log_tail": self._capture.drain(task_id)}
            self._store.update(record)
            set_current_task(None)
            with self._lock:
                self._current_task_id = None

    def get(self, task_id: str) -> TaskRecord | None:
        return self._store.get(task_id)

    def list(self, **filters: Any) -> tuple[list[TaskRecord], int]:
        return self._store.list(**filters)

    def active_task(self) -> TaskRecord | None:
        """供路由层组装 409 详情。内存优先，库兜底。"""
        with self._lock:
            task_id = self._current_task_id
        if task_id is not None:
            record = self._store.get(task_id)
            if record is not None and not record.status.is_terminal:
                return record
        return self._store.active()

    def wait_for_completion(self, task_id: str, timeout: float = 300) -> TaskRecord:
        """轮询到终态返回记录；超时抛 TimeoutError。

        **只供测试与调度器使用**——HTTP 路由调用它会把异步提交退化回同步阻塞（T3 禁）。
        """
        deadline = time.monotonic() + timeout
        while True:
            record = self._store.get(task_id)
            if record is None:
                raise KeyError(f"任务不存在：{task_id}")
            if record.status.is_terminal:
                return record
            if time.monotonic() >= deadline:
                raise TimeoutError(f"任务 {task_id} 在 {timeout}s 内未到达终态")
            time.sleep(0.05)
