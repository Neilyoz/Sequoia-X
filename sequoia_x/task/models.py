"""任务数据模型：类型/状态枚举、任务记录与单飞冲突异常。

status 枚举值与 triggered_by 取值集合是对外 JSON 契约（架构 §3.4），一旦定下不得改名。
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

# 时区名与 Settings.timezone 同源读环境变量，但本模块刻意不 import 配置单例：
# 任务层到处零散生成时间戳，若每处 get_settings() 就把"bootstrap 必须先行"的
# 顺序约束（约束 §1）渗进任务层内部了。
TIMEZONE_NAME: str = os.environ.get("TIMEZONE", "Asia/Shanghai")

_UTC8 = timezone(timedelta(hours=8))


def _get_tz() -> timezone:
    """解析 TIMEZONE_NAME 为 tzinfo；不可用时回退固定 UTC+8。

    Windows 的精简 Python 发行版可能不带 IANA tz 数据库（tzdata 包未装时
    ZoneInfo("Asia/Shanghai") 直接抛 ZoneInfoNotFoundError）。中国无夏令时，
    固定 UTC+8 用于展示与排序是安全的。
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        return _UTC8


def _now_iso() -> str:
    """ISO8601 带 +08:00 偏移的时间戳（架构 §4 响应约定的时间格式）。"""
    return datetime.now(_get_tz()).isoformat(timespec="seconds")


# T2 §4.2 明确要求 `str, Enum` 双继承（对外 JSON 契约的地基，改名/换基类都算破契约）；
# ruff 的 UP042 建议 StrEnum 行为等价但不在任务文档授权范围内，故按契约压建议。
class TaskKind(str, Enum):  # noqa: UP042
    DAILY = "daily"
    BACKFILL = "backfill"


class TaskStatus(str, Enum):  # noqa: UP042
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """终态不会再被推进；单飞判定与轮询终止条件都依赖它。"""
        return self in (TaskStatus.SUCCESS, TaskStatus.FAILED)


@dataclass
class TaskRecord:
    """一次任务的全生命周期记录，与 task_run 表行一一对应。"""

    task_id: str
    kind: TaskKind
    status: TaskStatus
    triggered_by: str
    params: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """对外契约形态：枚举展开为 .value，时间保持 ISO 字符串。

        不用 asdict 是因为它会把枚举原样留着，json.dumps 随即炸掉。
        """
        return {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "triggered_by": self.triggered_by,
            "params": self.params,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskAlreadyRunning(Exception):
    """单飞冲突（约束 §3）：已有 pending/running 任务时拒绝新提交，不排队。

    携带在跑任务的 id 与 kind，供路由层原样组装 409 响应体。
    """

    def __init__(self, running_task_id: str, kind: TaskKind) -> None:
        super().__init__(f"任务 {running_task_id}（{kind.value}）正在执行中，拒绝并发提交")
        self.running_task_id = running_task_id
        self.kind = kind
