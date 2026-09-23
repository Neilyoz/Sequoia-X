"""查询路由：/api/signals、/api/tasks/{id}/signals、/api/market/*（T5，纯本地只读）。

本文件的纪律（约束 §5 / T5 §4.4）：
- 全部语句都是 SELECT，且行情库/tasks.db 的自定义连接一律用 sqlite3 URI
  ``mode=ro`` 打开——即使代码写错也无法写入，双保险守住 456MB 真实行情库；
- 不触达任何 sync_*/baostock 路径：查询接口必须廉价、可高频调
  （约束 §3 的并发代价决定了新鲜度靠跑批任务维护，而不是查询顺手补数据）；
- signal 表在这里直接查而不复用 ``TaskStore.query_signals``：store 层没有
  task_id 过滤、没有 COUNT(total)、排序是 id DESC，三者都是 T5 §4.3 的硬要求，
  而本任务禁止改动 task/**（差异已如实上报，见交付报告"文档矛盾点"）。
"""

import re
import sqlite3
from datetime import date as _date
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sequoia_x.api.deps import get_engine, get_store, require_auth
from sequoia_x.api.schemas import (
    OhlcvResponse,
    SignalItem,
    SignalListResponse,
    StockBasicResponse,
    TaskSignalsResponse,
)
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.task.store import TaskStore

# 复用飞书卡片的雪球映射（feishu.py 的 _to_xueqiu_code）：T5 §4.3 明确要求
# "避免同一代码在卡片和 API 里两种写法"，单点实现优于抄一份 4 行逻辑。
_to_xueqiu_code = FeishuNotifier._to_xueqiu_code

# symbol 只允许 6 位纯数字（T5 §4.1）：虽然是参数化查询没有注入面，但该值
# 同时决定日志/上游映射/文件名，格式校验属于入口层职责，不进 DataEngine。
_SYMBOL_RE = re.compile(r"^\d{6}$")
# 日期必须恰好是 YYYY-MM-DD：正则先拦格式，再交给 fromisoformat 验真实日期
# （2026-02-30 过得了正则过不了日历，塞进 SQL 只会被 SQLite 静默比错）。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 排序固定 trade_date DESC, strategy, symbol（T5 §4.3），末尾追加 id 是
# 三重排序键并列时的稳定兜底——SQLite 无全序时同 offset 可能返回不同行，
# 分页结果不稳定是隐蔽 bug。
_SIGNAL_ORDER_BY = " ORDER BY trade_date DESC, strategy, symbol, id"

# 单任务信号"不分页"（T5 §4.3），LIMIT 仍要带（约束 §5）：几十条的常态下
# 1000 永远不截断，只防脏数据把响应撑爆。
_TASK_SIGNALS_LIMIT = 1000

router = APIRouter(prefix="/api", tags=["queries"], dependencies=[Depends(require_auth)])


def _read_only_connection(db_path: str) -> sqlite3.Connection:
    """以 mode=ro 打开 SQLite：本模块唯一允许的连接方式（约束 §5 只读规则）。"""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _require_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=400, detail={"code": "invalid_symbol", "symbol": symbol})
    return symbol


def _require_date(value: str, field: str) -> str:
    if not _DATE_RE.match(value):
        raise HTTPException(
            status_code=400, detail={"code": "invalid_date", "field": field, "value": value}
        )
    try:
        _date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_date", "field": field, "value": value}
        ) from exc
    return value


def _stock_daily_is_empty(db_path: str) -> bool:
    """stock_daily 是否一行都没有（T5 §4.4：空库最容易被误认为"接口坏了"）。

    EXISTS + LIMIT 1 是 O(1) 判定，绝不 COUNT(*) 扫 456MB 全表。
    """
    with _read_only_connection(db_path) as conn:
        return not conn.execute("SELECT EXISTS(SELECT 1 FROM stock_daily LIMIT 1)").fetchone()[0]


def _signal_items(engine: DataEngine, rows: list[sqlite3.Row]) -> list[SignalItem]:
    """把 signal 行组装成对外条目；name 一次 IN 查询批量补齐，严禁 N+1（T5 §4.3）。"""
    symbols = sorted({row["symbol"] for row in rows})
    names = engine.get_stock_names(symbols)  # 空列表返回 {}，已确认无需分支
    return [
        SignalItem(
            trade_date=row["trade_date"],
            strategy=row["strategy"],
            webhook_key=row["webhook_key"],
            symbol=row["symbol"],
            name=names.get(row["symbol"]),
            xueqiu_code=_to_xueqiu_code(row["symbol"]),
        )
        for row in rows
    ]


def _query_signals(
    store: TaskStore,
    where: list[str],
    args: list[Any],
    *,
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    """signal 表分页查询 + 过滤后总数，一次连接内完成，全部参数化。"""
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with _read_only_connection(store.db_path) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM signal{clause}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT task_id, trade_date, strategy, webhook_key, symbol FROM signal{clause}"
            f"{_SIGNAL_ORDER_BY} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
    return rows, total


@router.get(
    "/signals",
    response_model=SignalListResponse,
    description="跨任务选股结果。日期参数（date 或 start/end）至少要给一个，"
    "否则等于全表扫，返回 400 missing_filter（T5 §4.3 的防误用约定）。",
)
def list_signals(
    request: Request,
    trade_date: str | None = Query(
        None, alias="date", description="单日精确匹配，YYYY-MM-DD"
    ),
    start: str | None = Query(None, description="trade_date 闭区间起点，YYYY-MM-DD"),
    end: str | None = Query(None, description="trade_date 闭区间终点，YYYY-MM-DD"),
    strategy: str | None = Query(
        None, description="类名或 webhook_key 都接受（signal 落库的是类名）"
    ),
    symbol: str | None = Query(None, description="6 位纯数字代码"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    """GET /api/signals：看板"近 7/30 天"筛选的数据底座（T5 §4.3）。"""
    # 冲突检查先于缺失检查：date 与 start/end 同时给说明调用方语义混乱，
    # 静默取舍会让前端调试时抓不到为什么结果不对（T5 §4.3）。
    if trade_date is not None and (start is not None or end is not None):
        raise HTTPException(
            status_code=400,
            detail={"code": "conflicting_filters", "hint": "date 与 start/end 二选一"},
        )
    if trade_date is None and start is None and end is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_filter", "hint": "date 或 start/end 至少给一个"},
        )
    where: list[str] = []
    args: list[Any] = []
    if trade_date is not None:
        where.append("trade_date = ?")
        args.append(_require_date(trade_date, "date"))
    if start is not None:
        where.append("trade_date >= ?")
        args.append(_require_date(start, "start"))
    if end is not None:
        where.append("trade_date <= ?")
        args.append(_require_date(end, "end"))
    if strategy is not None:
        # 落库列 strategy 是类名、webhook_key 是路由标识；一个值同时比两列，
        # 类名与 webhook_key 两种写法天然等价，且不依赖注册表（历史策略
        # 即使已下线也查得到）。
        where.append("(strategy = ? OR webhook_key = ?)")
        args.extend([strategy, strategy])
    if symbol is not None:
        where.append("symbol = ?")
        args.append(_require_symbol(symbol))
    rows, total = _query_signals(
        get_store(request), where, args, limit=limit, offset=offset
    )
    return {
        "items": _signal_items(get_engine(request), rows),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/tasks/{task_id}/signals", response_model=TaskSignalsResponse)
def task_signals(task_id: str, request: Request) -> dict[str, object]:
    """单任务的选股信号明细（T5 §4.3）：不分页，任务不存在时返回空列表——
    与 GET /api/tasks/{id} 的 404 不同，这里不重复校验任务存在性，
    空 signal 行本身就是"该任务没有任何信号"的诚实回答。"""
    with _read_only_connection(get_store(request).db_path) as conn:
        rows = conn.execute(
            "SELECT task_id, trade_date, strategy, webhook_key, symbol FROM signal"
            " WHERE task_id = ?" + _SIGNAL_ORDER_BY + " LIMIT ?",
            (task_id, _TASK_SIGNALS_LIMIT),
        ).fetchall()
    items = _signal_items(get_engine(request), rows)
    return {"task_id": task_id, "items": items, "total": len(items)}


@router.get("/market/{symbol}/ohlcv", response_model=OhlcvResponse)
def market_ohlcv(
    symbol: str,
    request: Request,
    start: str | None = Query(None, description="闭区间起点，YYYY-MM-DD"),
    end: str | None = Query(None, description="闭区间终点，YYYY-MM-DD"),
    limit: int = Query(250, ge=1, le=500, description="最多返回条数（约束 §5 的 LIMIT 硬要求）"),
) -> dict[str, object]:
    """单只日线（升序）。只读本地缓存，绝不触发 baostock 同步（T5 §4.4）。"""
    _require_symbol(symbol)
    if start is not None:
        _require_date(start, "start")
    if end is not None:
        _require_date(end, "end")
    engine = get_engine(request)
    df: pd.DataFrame = engine.get_ohlcv_range(symbol, start=start, end=end, limit=limit)
    if df.empty and _stock_daily_is_empty(engine.db_path):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "database_not_seeded",
                "hint": "本地无行情数据，请先 POST /api/tasks/backfill "
                "或执行 python main.py --backfill",
            },
        )
    return {
        "symbol": symbol,
        "xueqiu_code": _to_xueqiu_code(symbol),
        "items": df.to_dict("records"),
        "total": len(df),
    }


@router.get("/market/{symbol}/basic", response_model=StockBasicResponse)
def market_basic(symbol: str, request: Request) -> dict[str, object]:
    """股票基础信息（本地 stock_basic）。未收录代码返回 200 + name=null：
    缺名称不等于代码非法（退市/新股都会出现），404 反而误导调用方。"""
    _require_symbol(symbol)
    names = get_engine(request).get_stock_names([symbol])
    return {
        "symbol": symbol,
        "name": names.get(symbol),
        "xueqiu_code": _to_xueqiu_code(symbol),
    }
