"""tasks.db 读写层：task_run 与 signal 两张表。

独立库的理由（约束 §5）：跑批路径里有 DELETE FROM stock_daily 这类破坏性写入，
任务表与 456MB 行情库同库混放会让"误删"和"膨胀"两类风险互相污染。
每次操作独立 connect，与 DataEngine 风格一致：任务库写频率极低，不值得上连接池。
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from sequoia_x.runner.pipeline import StrategyOutcome
from sequoia_x.task.models import TaskKind, TaskRecord, TaskStatus, _now_iso

# DDL 逐字照抄架构 §3.5（含索引名）。新增表只允许出现在 tasks.db。
_CREATE_TASK_RUN_SQL = """
CREATE TABLE IF NOT EXISTS task_run (
    task_id      TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    params_json  TEXT,
    result_json  TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
"""

_CREATE_SIGNAL_SQL = """
CREATE TABLE IF NOT EXISTS signal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    trade_date  TEXT NOT NULL,          -- 该信号对应的行情日期
    strategy    TEXT NOT NULL,          -- 策略类名
    webhook_key TEXT NOT NULL,
    symbol      TEXT NOT NULL,          -- 纯数字代码
    created_at  TEXT NOT NULL,
    UNIQUE (task_id, strategy, symbol)
);
"""

_CREATE_INDEX_SQLS = [
    "CREATE INDEX IF NOT EXISTS idx_signal_date ON signal (trade_date, strategy);",
    "CREATE INDEX IF NOT EXISTS idx_signal_task ON signal (task_id);",
    "CREATE INDEX IF NOT EXISTS idx_task_run_state ON task_run (status, created_at);",
]

_TASK_RUN_COLUMNS = (
    "task_id",
    "kind",
    "status",
    "triggered_by",
    "params_json",
    "result_json",
    "error",
    "created_at",
    "started_at",
    "finished_at",
)


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        kind=TaskKind(row["kind"]),
        status=TaskStatus(row["status"]),
        triggered_by=row["triggered_by"],
        params=json.loads(row["params_json"]) if row["params_json"] else {},
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class TaskStore:
    """tasks.db 的存取封装。全部方法同步、无共享状态，可在锁外安全调用。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # 目录可能不存在（全新 clone 只有 data/ 里有行情库的场合也要保证可创建）。
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        """建表建索引，幂等。服务启动时调用一次。"""
        with self._connect() as conn:
            conn.execute(_CREATE_TASK_RUN_SQL)
            conn.execute(_CREATE_SIGNAL_SQL)
            for ddl in _CREATE_INDEX_SQLS:
                conn.execute(ddl)

    def create(self, record: TaskRecord) -> None:
        """插入新任务记录（task_id 冲突会抛 IntegrityError——uuid4 场景不会撞）。"""
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO task_run ({', '.join(_TASK_RUN_COLUMNS)})"
                f" VALUES ({', '.join('?' * len(_TASK_RUN_COLUMNS))})",
                self._values(record),
            )

    def update(self, record: TaskRecord) -> None:
        """按 task_id upsert 全字段。

        选 ON CONFLICT 单一路径而非"UPDATE + 插不中再 INSERT"两段式，
        避免并发写下的丢失更新窗口（架构 §3.5 要求二选一）。
        """
        columns = ", ".join(_TASK_RUN_COLUMNS)
        placeholders = ", ".join("?" * len(_TASK_RUN_COLUMNS))
        sql = (
            f"INSERT INTO task_run ({columns}) VALUES ({placeholders})"
            " ON CONFLICT(task_id) DO UPDATE SET"
            " kind = excluded.kind, status = excluded.status,"
            " triggered_by = excluded.triggered_by, params_json = excluded.params_json,"
            " result_json = excluded.result_json, error = excluded.error,"
            " created_at = excluded.created_at, started_at = excluded.started_at,"
            " finished_at = excluded.finished_at"
        )
        with self._connect() as conn:
            conn.execute(sql, self._values(record))

    @staticmethod
    def _values(record: TaskRecord) -> tuple:
        return (
            record.task_id,
            record.kind.value,
            record.status.value,
            record.triggered_by,
            json.dumps(record.params, ensure_ascii=False),
            json.dumps(record.result, ensure_ascii=False) if record.result is not None else None,
            record.error,
            record.created_at,
            record.started_at,
            record.finished_at,
        )

    def get(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_run WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def list(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TaskRecord], int]:
        """按 kind/status 过滤的历史列表（新→旧），返回 (页内记录, 过滤后总数)。"""
        where: list[str] = []
        args: list[Any] = []
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if status is not None:
            where.append("status = ?")
            args.append(status)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM task_run{clause}", args).fetchone()[0]
            rows = conn.execute(
                # created_at 秒级精度会并列，rowid 作稳定次序保证分页不重不漏
                f"SELECT * FROM task_run{clause} ORDER BY created_at DESC, rowid DESC"
                " LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return [_row_to_record(r) for r in rows], total

    def active(self) -> TaskRecord | None:
        """非终态任务。单飞判定的兜底：进程重启后内存标志丢失时靠它。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_run WHERE status IN ('pending','running')"
                " ORDER BY created_at LIMIT 1"
            ).fetchone()
        return _row_to_record(row) if row else None

    def save_signals(
        self, task_id: str, trade_date: str, outcomes: list[StrategyOutcome]
    ) -> int:
        """把 DailyReport.outcomes 摊平成 signal 行写入，返回实际插入行数。

        INSERT OR IGNORE 配合 UNIQUE(task_id, strategy, symbol)：重复落库幂等，
        任务重试同一天不会堆出双份信号。
        """
        now = _now_iso()
        rows = [
            (task_id, trade_date, o.strategy_name, o.webhook_key, symbol, now)
            for o in outcomes
            for symbol in o.symbols
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO signal"
                " (task_id, trade_date, strategy, webhook_key, symbol, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return cur.rowcount

    def query_signals(
        self,
        *,
        trade_date: str | None = None,
        start: str | None = None,
        end: str | None = None,
        strategy: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """跨任务查选股结果（T5 看板与 T8 前端日期区间选择器的数据底座）。

        start/end 是 trade_date 的闭区间（含端点），与 trade_date 互斥使用由调用方保证；
        过滤条件全部参数化，防注入。
        """
        where: list[str] = []
        args: list[Any] = []
        for column, value in (
            ("trade_date", trade_date),
            ("strategy", strategy),
            ("symbol", symbol),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if start is not None:
            where.append("trade_date >= ?")
            args.append(start)
        if end is not None:
            where.append("trade_date <= ?")
            args.append(end)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT task_id, trade_date, strategy, webhook_key, symbol, created_at"
                f" FROM signal{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return [dict(r) for r in rows]

    def prune(self, keep: int) -> None:
        """只保留最近 keep 条任务，信号级联清理。绝不触碰行情表（约束 §5）。"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM task_run WHERE task_id NOT IN"
                " (SELECT task_id FROM task_run ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (keep,),
            )
            conn.execute(
                "DELETE FROM signal WHERE task_id NOT IN (SELECT task_id FROM task_run)"
            )

    def recover_interrupted(self) -> int:
        """把上次进程遗留的非终态记录一律标记失败，返回受影响条数。

        不这样做的话，脏的 running 记录会让 active() 兜底把单飞锁永久占住。
        finished_at 选择填恢复时刻而非留空：与 _execute 的终态语义一致
        （凡终态必有完成时间，消费方不必对 started/finished 为空做分支）。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE task_run SET status = 'failed',"
                " error = '进程重启导致任务中断', finished_at = ?"
                " WHERE status IN ('pending','running')",
                (_now_iso(),),
            )
        return cur.rowcount
