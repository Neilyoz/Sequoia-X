"""T5 查询接口测试（T5 §5）。

全部库文件都在 tmp_path 下独立新建，绝不碰 data/sequoia_v2.db 真实行情库；
行情/信号数据由 fixture 手写 SQL 与真 TaskStore.save_signals 落库，
不触网、不跑批（sync_* 被 patch 后断言零调用，见文件末尾只读性质测试）。

app.include_router(queries.router) 模拟主 agent 即将写入 app.py 的唯一一行接线
（本任务禁止改动 app.py），其余对象全部来自真 lifespan。
"""

import hashlib
import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from sequoia_x.api.app import create_app
from sequoia_x.api.routes import queries
from sequoia_x.core.config import Settings
from sequoia_x.runner.pipeline import StrategyOutcome

API_KEY = "sekret"
AUTH = {"X-API-Key": API_KEY}

# 3 只股票 × 5 个交易日（2026-01-05 ~ 01-09）
DAILY_SYMBOLS = {"600000": 10.0, "000001": 20.0, "830799": 30.0}
TRADE_DATES = [f"2026-01-{d:02d}" for d in range(5, 10)]

# 信号批次：(task_id, trade_date, 类名, webhook_key, symbols)。
# 2026-01-05 与 2026-01-09 各有一批，专门用于闭区间边界断言；
# 01-02 与 01-16 是区间外的对照行。
SIGNAL_BATCHES = [
    ("t1", "2026-01-05", "MaVolumeStrategy", "ma_volume", ["600000", "000001"]),
    ("t2", "2026-01-09", "TurtleTradeStrategy", "turtle", ["600000", "600519"]),
    ("t3", "2026-01-08", "HighTightFlagStrategy", "flag", ["830799"]),
    ("t4", "2026-01-02", "MaVolumeStrategy", "ma_volume", ["000001"]),
    ("t5", "2026-01-16", "RpsBreakoutStrategy", "rps", ["000001"]),
]


def _detach_task_log_captures() -> None:
    """摘除 lifespan 挂上的 TaskLogCapture（与 test_api_tasks.py 同一手法）：
    不清扫会让 test_logger.py 的属性测试假红。"""
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


def make_client(tmp_path: Path):
    """起真 lifespan 的 TestClient；行情库/任务库都是 tmp 下的小空库。"""
    settings = Settings(
        _env_file=None,
        feishu_webhook_url="http://feishu.invalid/hook",
        db_path=str(tmp_path / "market.db"),
        task_db_path=str(tmp_path / "tasks.db"),
        api_key=API_KEY,
    )
    app = create_app(settings)
    app.include_router(queries.router)  # 与主 agent 将写入 app.py 的接线保持一致
    return app, TestClient(app)


def seed_market(db_path: str) -> None:
    """向 tmp 行情库插 3 只 × 5 天日线 + 2 条股票名称（830799 刻意不收录，测 null）。"""
    with sqlite3.connect(db_path) as conn:
        for symbol, base in DAILY_SYMBOLS.items():
            for i, d in enumerate(TRADE_DATES):
                conn.execute(
                    "INSERT INTO stock_daily"
                    " (symbol, date, open, high, low, close, volume, turnover)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, d, base + i, base + i + 1, base + i - 1, base + i + 0.5,
                     1000.0 + i, 12345.0 + i),
                )
        conn.executemany(
            "INSERT INTO stock_basic (symbol, name, updated_at) VALUES (?, ?, ?)",
            [("600000", "浦发银行", "2026-01-09"), ("000001", "平安银行", "2026-01-09")],
        )


def seed_signals(store) -> None:
    """用真 TaskStore.save_signals 落库，保证列语义（类名+webhook_key）与跑批一致。"""
    for task_id, trade_date, cls, key, symbols in SIGNAL_BATCHES:
        store.save_signals(
            task_id,
            trade_date,
            [StrategyOutcome(strategy_name=cls, webhook_key=key, symbols=symbols, pushed=False)],
        )


@pytest.fixture
def client(tmp_path):
    """带完整种子数据的客户端。

    种子必须在 lifespan 之后写入：建表的是 lifespan 里的 DataEngine/_TaskStore，
    提前写会撞上 no such table / app.state.store is None。
    """
    app, tc = make_client(tmp_path)
    with tc:
        seed_market(app.state.settings.db_path)
        seed_signals(app.state.store)
        yield tc


@pytest.fixture
def empty_client(tmp_path):
    """两库皆空（只有 lifespan 建好的 schema）的客户端，测 database_not_seeded。"""
    _, tc = make_client(tmp_path)
    with tc:
        yield tc


def keys_of(items: list[dict]) -> list[tuple]:
    return [(i["trade_date"], i["strategy"], i["symbol"]) for i in items]


# ── /api/signals ──


def test_signals_by_date_returns_total_and_names(client) -> None:
    """守住的性质：单日过滤条数/total 正确，name 由 stock_basic 批量补齐，
    未收录代码 name 为 null（T5 §4.3）。"""
    resp = client.get("/api/signals", params={"date": "2026-01-05"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2 and len(body["items"]) == 2
    by_symbol = {i["symbol"]: i for i in body["items"]}
    assert by_symbol["600000"]["name"] == "浦发银行"
    assert by_symbol["000001"]["name"] == "平安银行"
    assert body["limit"] == 100 and body["offset"] == 0


def test_signals_closed_interval_includes_both_boundaries(client) -> None:
    """守住的性质：start/end 是闭区间——边界日 01-05/01-09 各造一批，
    断言两条都在、区间外（01-02/01-16）都不在（T5 §5）。"""
    resp = client.get(
        "/api/signals", params={"start": "2026-01-05", "end": "2026-01-09"}, headers=AUTH
    )
    body = resp.json()
    assert body["total"] == 5  # t1×2 + t2×2 + t3×1
    dates = {i["trade_date"] for i in body["items"]}
    assert dates == {"2026-01-05", "2026-01-08", "2026-01-09"}
    assert "2026-01-02" not in dates and "2026-01-16" not in dates


def test_signals_requires_at_least_one_date_filter(client) -> None:
    """守住的性质：无任何日期过滤 → 400 missing_filter（全表扫防误用，T5 §4.3）。"""
    resp = client.get("/api/signals", headers=AUTH)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "missing_filter"


def test_signals_date_plus_range_is_conflicting(client) -> None:
    """守住的性质：date 与 start/end 同时给 → 400 conflicting_filters，
    不静默取舍（前端调试时抓得到原因，T5 §4.3）。"""
    resp = client.get(
        "/api/signals",
        params={"date": "2026-01-05", "start": "2026-01-01"},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "conflicting_filters"


def test_signals_rejects_bad_date_format(client) -> None:
    """守住的性质：非 YYYY-MM-DD 或日历非法的日期 → 400 invalid_date，
    不塞进 SQL 让 SQLite 静默比错（T5 §4.2/§4.3）。"""
    for bad in ("20260105", "2026-1-5", "not-a-date", "2026-02-30"):
        resp = client.get("/api/signals", params={"date": bad}, headers=AUTH)
        assert resp.status_code == 400, bad
        assert resp.json()["detail"]["code"] == "invalid_date"


def test_signals_strategy_class_and_webhook_key_equivalent(client) -> None:
    """守住的性质：strategy 用类名与用 webhook_key 结果完全一致（T5 §4.3）。"""
    by_class = client.get(
        "/api/signals",
        params={"date": "2026-01-05", "strategy": "MaVolumeStrategy"},
        headers=AUTH,
    ).json()
    by_key = client.get(
        "/api/signals",
        params={"date": "2026-01-05", "strategy": "ma_volume"},
        headers=AUTH,
    ).json()
    assert by_class["total"] == 2
    assert keys_of(by_class["items"]) == keys_of(by_key["items"])


def test_signals_symbol_filter_and_invalid(client) -> None:
    """symbol 过滤生效；非法代码 400 invalid_symbol（校验在入口层，T5 §4.1）。"""
    body = client.get(
        "/api/signals", params={"start": "2026-01-01", "end": "2026-01-31",
                                "symbol": "600000"},
        headers=AUTH,
    ).json()
    assert body["total"] == 2  # t1（01-05）与 t2（01-09）各一条 600000
    assert all(i["symbol"] == "600000" for i in body["items"])
    resp = client.get(
        "/api/signals", params={"date": "2026-01-05", "symbol": "12345"}, headers=AUTH
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_symbol"


def test_signals_pagination_no_dup_no_gap(client) -> None:
    """守住的性质：limit=2 遍历全部，各页拼接与不分页结果逐一相同且顺序稳定
    （ORDER BY trade_date DESC, strategy, symbol，T5 §4.3）。"""
    full = client.get(
        "/api/signals", params={"start": "2026-01-01", "end": "2026-01-31"}, headers=AUTH
    ).json()
    assert full["total"] == 7
    # 显式断言全局排序：新日期在前，同日按策略类名、再按 symbol
    assert keys_of(full["items"]) == [
        ("2026-01-16", "RpsBreakoutStrategy", "000001"),
        ("2026-01-09", "TurtleTradeStrategy", "600000"),
        ("2026-01-09", "TurtleTradeStrategy", "600519"),
        ("2026-01-08", "HighTightFlagStrategy", "830799"),
        ("2026-01-05", "MaVolumeStrategy", "000001"),
        ("2026-01-05", "MaVolumeStrategy", "600000"),
        ("2026-01-02", "MaVolumeStrategy", "000001"),
    ]
    paged: list[dict] = []
    for offset in range(0, 7, 2):
        page = client.get(
            "/api/signals",
            params={"start": "2026-01-01", "end": "2026-01-31", "limit": 2, "offset": offset},
            headers=AUTH,
        ).json()
        assert page["total"] == 7
        paged.extend(page["items"])
    assert keys_of(paged) == keys_of(full["items"])


def test_signals_rejects_limit_over_1000(client) -> None:
    """limit 硬上限 1000（约束 §5），超了是 pydantic/FastAPI 的 422 而非静默截断。"""
    resp = client.get(
        "/api/signals", params={"date": "2026-01-05", "limit": 1001}, headers=AUTH
    )
    assert resp.status_code == 422


# ── /api/tasks/{id}/signals ──


def test_task_signals_lists_items_without_pagination(client) -> None:
    """守住的性质：单任务信号明细返回该任务全部条目；不存在的任务返回空列表。"""
    body = client.get("/api/tasks/t1/signals", headers=AUTH).json()
    assert body["task_id"] == "t1" and body["total"] == 2
    assert {i["symbol"] for i in body["items"]} == {"600000", "000001"}
    missing = client.get("/api/tasks/nope/signals", headers=AUTH)
    assert missing.status_code == 200 and missing.json()["items"] == []


# ── /api/market/{symbol}/ohlcv ──


def test_ohlcv_ascending_by_default_and_limit_takes_latest(client) -> None:
    """守住的性质：默认全区间升序；limit 截断保留的是最近 N 天再升序返回
    （倒序取、正序给，T5 §4.2）。"""
    body = client.get("/api/market/600000/ohlcv", headers=AUTH).json()
    assert body["total"] == 5
    assert [i["date"] for i in body["items"]] == TRADE_DATES
    tail = client.get("/api/market/600000/ohlcv", params={"limit": 2}, headers=AUTH).json()
    assert [i["date"] for i in tail["items"]] == TRADE_DATES[-2:]


def test_ohlcv_interval_is_closed(client) -> None:
    """守住的性质：start/end 闭区间，两端日都在结果里。"""
    body = client.get(
        "/api/market/000001/ohlcv",
        params={"start": "2026-01-06", "end": "2026-01-08"},
        headers=AUTH,
    ).json()
    assert [i["date"] for i in body["items"]] == ["2026-01-06", "2026-01-07", "2026-01-08"]


def test_ohlcv_rejects_bad_params(client) -> None:
    """limit 上限 500（le=500 → 422）；start/end 非 YYYY-MM-DD → 400 invalid_date。"""
    assert client.get(
        "/api/market/600000/ohlcv", params={"limit": 9999}, headers=AUTH
    ).status_code == 422
    resp = client.get(
        "/api/market/600000/ohlcv", params={"start": "2026/01/05"}, headers=AUTH
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_date"


def test_ohlcv_rejects_invalid_symbol(client) -> None:
    """守住的性质：5 位数字与字母代码都 400 invalid_symbol（T5 §4.1）。"""
    for bad in ("12345", "abcdef"):
        resp = client.get(f"/api/market/{bad}/ohlcv", headers=AUTH)
        assert resp.status_code == 400, bad
        assert resp.json()["detail"]["code"] == "invalid_symbol"


def test_sql_injection_probe_rejected_and_table_survives(client, tmp_path) -> None:
    """守住的性质：注入探针 → 400 且 stock_daily 表还在（T5 §5，参数化 +
    入口层正则的双保险，任何一层失效都不会丢表）。"""
    probe = "000001'; DROP TABLE stock_daily; --"
    resp = client.get(f"/api/market/{quote(probe, safe='')}/ohlcv", headers=AUTH)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_symbol"
    with sqlite3.connect(str(tmp_path / "market.db")) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='stock_daily'"
        ).fetchone()[0]
    assert n == 1
    # 表不仅能查、数据也没少
    assert client.get("/api/market/600000/ohlcv", headers=AUTH).json()["total"] == 5


def test_ohlcv_empty_db_returns_409_not_seeded(empty_client) -> None:
    """守住的性质：stock_daily 为空 → 409 database_not_seeded 带下一步指引
    （空库最容易被误认为接口坏了，T5 §4.4）。"""
    resp = empty_client.get("/api/market/000001/ohlcv", headers=AUTH)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "database_not_seeded"
    assert "backfill" in detail["hint"]


def test_ohlcv_unknown_symbol_in_seeded_db_returns_empty_200(client) -> None:
    """守住的性质：库里有行情但该代码无数据 → 200 空列表，而不是误报 409。"""
    body = client.get("/api/market/999999/ohlcv", headers=AUTH).json()
    assert body["total"] == 0 and body["items"] == []


# ── /api/market/{symbol}/basic 与雪球映射 ──


def test_basic_returns_name_and_null_for_unknown(client) -> None:
    """stock_basic 收录 → 带名称；未收录但格式合法 → 200 + name=null（缺名称
    不等于代码非法，不返回 404）。"""
    body = client.get("/api/market/600000/basic", headers=AUTH).json()
    assert body["name"] == "浦发银行" and body["xueqiu_code"] == "SH600000"
    assert client.get("/api/market/999999/basic", headers=AUTH).json()["name"] is None
    assert client.get("/api/market/12345/basic", headers=AUTH).status_code == 400


def test_xueqiu_code_mapping_matches_feishu(client) -> None:
    """守住的性质：600519→SH、000001→SZ、830799→BJ，与 feishu.py 卡片同一套
    映射（同一代码不允许卡片和 API 两种写法，T5 §4.3）。"""
    signals = client.get(
        "/api/signals", params={"start": "2026-01-01", "end": "2026-01-31"}, headers=AUTH
    ).json()
    xq = {i["symbol"]: i["xueqiu_code"] for i in signals["items"]}
    assert xq["600519"] == "SH600519" and xq["000001"] == "SZ000001"
    assert xq["830799"] == "BJ830799"
    assert client.get("/api/market/830799/basic", headers=AUTH).json()["xueqiu_code"] == "BJ830799"
    assert client.get("/api/market/600000/ohlcv", headers=AUTH).json()["xueqiu_code"] == "SH600000"


# ── 鉴权与只读性质 ──


def test_all_query_routes_require_api_key(client) -> None:
    """router 级 Depends(require_api_key)：四个查询接口缺 key 一律 401（T5 §4.5）。"""
    for url in (
        "/api/signals?date=2026-01-05",
        "/api/tasks/t1/signals",
        "/api/market/600000/ohlcv",
        "/api/market/600000/basic",
    ):
        assert client.get(url).status_code == 401, url


def test_query_routes_never_touch_network(client, tmp_path) -> None:
    """守住的性质（约束 §3 / T5 §4.4）：查询全程 sync_* 零调用，
    且行情库文件字节哈希前后一致——纯本地只读，没有任何写副作用。"""
    market_db = tmp_path / "market.db"
    hash_before = hashlib.sha256(market_db.read_bytes()).hexdigest()
    with (
        patch("sequoia_x.data.engine.DataEngine.sync_today_bulk") as bulk,
        patch("sequoia_x.data.engine.DataEngine.sync_stock_basic") as basic,
        patch("sequoia_x.data.engine.DataEngine.backfill") as backfill,
    ):
        resp = client.get("/api/signals", params={"date": "2026-01-05"}, headers=AUTH)
        assert resp.status_code == 200
        assert client.get("/api/tasks/t1/signals", headers=AUTH).status_code == 200
        assert client.get("/api/market/600000/ohlcv", headers=AUTH).status_code == 200
        assert client.get("/api/market/600000/basic", headers=AUTH).status_code == 200
    assert bulk.called is False and basic.called is False and backfill.called is False
    assert hashlib.sha256(market_db.read_bytes()).hexdigest() == hash_before
