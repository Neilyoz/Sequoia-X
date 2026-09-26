"""数据引擎属性测试。"""

import gc
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data import engine as engine_module
from sequoia_x.data.engine import DataEngine


@pytest.fixture(autouse=True)
def _reset_bs_login_state(tmp_path, monkeypatch):
    """登录预算与磁盘熔断都是跨用例状态，必须清零并隔离。

    熔断状态必须落到临时目录：否则用例之间互相污染，也会往项目 data/ 里写文件。
    """
    monkeypatch.setenv(engine_module.bs_breaker._ENV_PATH, str(tmp_path / "breaker.json"))
    engine_module.bs_reset_login_budget()
    yield
    engine_module.bs_reset_login_budget()


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        # `with conn` 只提交事务不关连接；3.14 的 sqlite3 连接又靠 GC 兜底关闭，
        # 句柄不释放的话 Windows 下 TemporaryDirectory 清理会抛 PermissionError。
        conn.close()
        gc.collect()
        assert count == 1


# Property: 股票名称入库后本地可读、重名会覆盖更新
def test_stock_basic_persists_and_updates_names(tmp_path: Path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "names.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)

    assert engine.get_stock_names(["600000", "000001"]) == {}

    engine._save_stock_basic([("600000", "浦发银行"), ("000001", "平安银行")])
    assert engine.get_stock_names(["600000", "000001"]) == {
        "600000": "浦发银行",
        "000001": "平安银行",
    }

    # 更名（如被 ST）后同步应覆盖旧名称；空名称不入库，保留原名称
    engine._save_stock_basic([("600000", "ST浦发"), ("000001", "")])
    assert engine.get_stock_names(["600000", "000001"]) == {
        "600000": "ST浦发",
        "000001": "平安银行",
    }

    assert engine.get_stock_names([]) == {}


def test_stock_basic_page_filters_by_symbol_or_name(tmp_path: Path) -> None:
    """列表关键词可按代码或名称子串筛选，并返回过滤后的总数。"""
    engine, _ = make_engine_in(str(tmp_path))
    engine._save_stock_basic(
        [
            ("000001", "平安银行"),
            ("600000", "浦发银行"),
            ("600519", "贵州茅台"),
            ("300750", "宁德时代"),
        ]
    )

    by_symbol, symbol_total, initialized = engine.get_stock_basic_page(
        keyword="000001", limit=50, offset=0
    )
    by_name, name_total, _ = engine.get_stock_basic_page(
        keyword="银行", limit=50, offset=0
    )

    assert by_symbol == [{"symbol": "000001", "name": "平安银行"}]
    assert symbol_total == 1
    assert by_name == [
        {"symbol": "000001", "name": "平安银行"},
        {"symbol": "600000", "name": "浦发银行"},
    ]
    assert name_total == 2
    assert initialized is True


def test_stock_basic_page_escapes_like_wildcards(tmp_path: Path) -> None:
    """LIKE 通配符字符作为普通搜索文本，不扩大匹配范围。"""
    engine, _ = make_engine_in(str(tmp_path))
    engine._save_stock_basic(
        [("000001", "百分%银行"), ("000002", "下划_银行"), ("000003", "平安银行")]
    )

    percent, _, _ = engine.get_stock_basic_page(keyword="%", limit=50, offset=0)
    underscore, _, _ = engine.get_stock_basic_page(keyword="_", limit=50, offset=0)

    assert percent == [{"symbol": "000001", "name": "百分%银行"}]
    assert underscore == [{"symbol": "000002", "name": "下划_银行"}]


def test_stock_basic_page_orders_and_paginates(tmp_path: Path) -> None:
    """列表代码升序分页，末页之外返回空项但保留总数。"""
    engine, _ = make_engine_in(str(tmp_path))
    engine._save_stock_basic(
        [("600519", "贵州茅台"), ("300750", "宁德时代"), ("600000", "浦发银行")]
    )

    items, total, initialized = engine.get_stock_basic_page(
        keyword=None, limit=2, offset=1
    )
    beyond, beyond_total, _ = engine.get_stock_basic_page(
        keyword=None, limit=2, offset=20
    )

    assert items == [
        {"symbol": "600000", "name": "浦发银行"},
        {"symbol": "600519", "name": "贵州茅台"},
    ]
    assert total == 3
    assert initialized is True
    assert beyond == []
    assert beyond_total == 3


# Property: sync_stock_basic 只收录上市股票，并把代码与名称一起写库
def test_sync_stock_basic_filters_and_saves_names(tmp_path: Path) -> None:
    class _FakeRs:
        error_code = "0"
        error_msg = "success"

        def __init__(self, rows: list[list[str]]) -> None:
            self._rows = rows
            self._i = -1

        def next(self) -> bool:
            self._i += 1
            return self._i < len(self._rows)

        def get_row_data(self) -> list[str]:
            return self._rows[self._i]

    rows = [
        ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],   # 上市股票
        ["sz.000001", "平安银行", "1991-04-03", "", "1", "1"],   # 上市股票
        ["sh.510300", "沪深300ETF", "2012-05-28", "", "5", "1"],  # 基金，应过滤
        ["sz.000002", "万科A", "1991-01-29", "2024-01-01", "1", "0"],  # 退市，应过滤
    ]

    engine = DataEngine(Settings(
        db_path=str(tmp_path / "basic.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    ))
    with patch("baostock.query_stock_basic", return_value=_FakeRs(rows)), \
         patch("baostock.logout"), \
         patch("sequoia_x.data.engine.bs_login", return_value=True):
        symbols = engine.sync_stock_basic()

    assert symbols == ["600000", "000001"]
    assert engine.get_stock_names(symbols) == {
        "600000": "浦发银行",
        "000001": "平安银行",
    }


# Property: 名称同步走 baostock 单源；baostock 挂时返回 (0, [])，不再有 akshare 兜底
def test_sync_stock_names_uses_baostock_only(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "names_bs.db")

    with patch("sequoia_x.data.engine.bs_login", return_value=False):
        saved, codes = engine.sync_stock_names()

    # baostock 不可用：无兜底源，空手而归
    assert saved == 0
    assert codes == []


# Property: baostock 不可用时不再退 akshare，直接回退本地已入库代码
def test_sync_stock_basic_falls_back_to_local_when_baostock_down(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "basic_fallback.db")
    # 本地已有历史代码，模拟此前跑批入库
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute(
            "INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover) "
            "VALUES ('600000', '2024-01-02', 1, 1, 1, 1, 1, 1)"
        )

    with patch("sequoia_x.data.engine.bs_login", return_value=False):
        symbols = engine.sync_stock_basic()

    assert symbols == ["600000"]


# Property: baostock 挂且日常链路（不回退本地）时，宁可空手也不拿历史代码当当日清单
def test_sync_stock_basic_skips_local_fallback_when_asked(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "basic_nolocal.db")
    # DataEngine 构造时已建好 stock_daily，这里塞一行模拟"本地有历史数据"
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute(
            "INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover) "
            "VALUES ('600000', '2024-01-02', 1, 1, 1, 1, 1, 1)"
        )

    with patch("sequoia_x.data.engine.bs_login", return_value=False):
        assert engine.sync_stock_basic(return_local_on_failure=False) == []
        # 默认行为仍是回退本地，回填链路需要它把流程从一次抖动里救回来
        assert engine.sync_stock_basic() == ["600000"]


class _FakeResult:
    """最小 ResultData：只实现 error_code 判定与逐行读取。"""

    def __init__(self, rows: list[list[str]], error_code: str = "0",
                 error_msg: str = "success", fields: list[str] | None = None) -> None:
        self._rows = rows
        self._i = -1
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = fields or []

    def next(self) -> bool:
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._i]


def _make_engine(tmp_path: Path, name: str) -> DataEngine:
    return DataEngine(Settings(
        db_path=str(tmp_path / name),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    ))


# 红线：一轮增量同步只允许一次 bs.login()（每次登录 = 一条新 TCP，密集登录触发黑名单）
def test_sync_today_bulk_uses_single_login(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "bulk.db")
    with sqlite3.connect(engine.db_path) as conn:
        for symbol in ["000001", "600000", "300750"]:
            conn.execute(
                "INSERT INTO stock_daily (symbol, date, close, volume)"
                " VALUES (?, '2024-01-02', 10.0, 100.0)",
                (symbol,),
            )
        conn.commit()

    logins: list[int] = []
    k_calls: list[dict] = []

    with patch("baostock.login", side_effect=lambda *a, **k: logins.append(1)
               or _FakeResult([], error_code="0")), \
         patch("baostock.logout"), \
         patch("baostock.query_trade_dates",
               return_value=_FakeResult([["2024-01-03", "1"], ["2024-01-04", "0"]])), \
         patch("baostock.query_history_k_data_plus",
               side_effect=lambda **kw: k_calls.append(kw) or _FakeResult(
                   [[kw["start_date"], "10", "11", "9", "10.5", "1000", "10500"]])):
        written = engine.sync_today_bulk()

    assert written == 3
    assert len(logins) == 1
    assert len(k_calls) == 3


# 休市日：日历说不必同步，就一次 K 线请求都不发
def test_sync_today_bulk_skips_on_non_trading_day(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "bulk_holiday.db")
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute(
            "INSERT INTO stock_daily (symbol, date, close, volume)"
            " VALUES ('000001', '2024-01-02', 10.0, 100.0)"
        )
        conn.commit()

    with patch("baostock.login", return_value=_FakeResult([], error_code="0")), \
         patch("baostock.logout"), \
         patch("baostock.query_trade_dates",
               return_value=_FakeResult([["2024-01-06", "0"], ["2024-01-07", "0"]])), \
         patch("baostock.query_history_k_data_plus") as k_mock:
        written = engine.sync_today_bulk()

    assert written == 0
    assert k_mock.called is False


# 未收盘时目标日不得取「今天」：否则查区间含空的当日，前一交易日的数据也拿不到
def test_sync_today_bulk_targets_last_closed_day_before_close(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "bulk_closed.db")
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute(
            "INSERT INTO stock_daily (symbol, date, close, volume)"
            " VALUES ('000001', '2026-09-22', 10.0, 100.0)"
        )
        conn.commit()

    k_calls: list[dict] = []

    class _FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return date(2026, 9, 24)

    with patch("sequoia_x.data.engine.date", _FixedDate), \
         patch("sequoia_x.data.engine._after_close", return_value=False), \
         patch("baostock.login", return_value=_FakeResult([], error_code="0")), \
         patch("baostock.logout"), \
         patch("baostock.query_trade_dates",
               return_value=_FakeResult([["2026-09-23", "1"], ["2026-09-24", "1"]])), \
         patch("baostock.query_history_k_data_plus",
               side_effect=lambda **kw: k_calls.append(kw) or _FakeResult(
                   [["2026-09-23", "10", "11", "9", "10.5", "1000", "10500"]])):
        written = engine.sync_today_bulk()

    # 未收盘 → 目标日退到 9-23；查询的 end_date 必须是 9-23 而不是今天
    assert written == 1
    assert len(k_calls) == 1
    assert k_calls[0]["start_date"] == "2026-09-23"
    assert k_calls[0]["end_date"] == "2026-09-23"
    with sqlite3.connect(engine.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE date = '2026-09-23'"
        ).fetchone()[0] == 1


# 单只查询抛异常（如超时）不得拖垮整轮：已取到的照常入库
def test_sync_today_bulk_survives_per_symbol_failure(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "bulk_partial.db")
    with sqlite3.connect(engine.db_path) as conn:
        for symbol in ["000001", "600000", "300750"]:
            conn.execute(
                "INSERT INTO stock_daily (symbol, date, close, volume)"
                " VALUES (?, '2024-01-02', 10.0, 100.0)",
                (symbol,),
            )
        conn.commit()

    def fake_k(**kw):
        if "600000" in kw["code"]:
            raise TimeoutError("timed out")
        return _FakeResult([[kw["start_date"], "10", "11", "9", "10.5", "1000", "10500"]])

    with patch("baostock.login", return_value=_FakeResult([], error_code="0")), \
         patch("baostock.logout"), \
         patch("baostock.query_trade_dates",
               return_value=_FakeResult([["2024-01-03", "1"]])), \
         patch("baostock.query_history_k_data_plus", side_effect=fake_k):
        written = engine.sync_today_bulk()

    # 600000 超时被跳过，另两只正常写入
    assert written == 2


# 连接中途断掉：连续失败达阈值时重连一次接着跑，而不是丢掉剩余全部
def test_sync_today_bulk_reconnects_after_consecutive_failures(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "bulk_reconnect.db")
    symbols = [f"{i:06d}" for i in range(1, 16)]
    with sqlite3.connect(engine.db_path) as conn:
        for symbol in symbols:
            conn.execute(
                "INSERT INTO stock_daily (symbol, date, close, volume)"
                " VALUES (?, '2024-01-02', 10.0, 100.0)",
                (symbol,),
            )
        conn.commit()

    logins: list[int] = []

    def fake_login(*a, **k):
        logins.append(1)
        return _FakeResult([], error_code="0")

    call_state = {"n": 0}

    def fake_k(**kw):
        call_state["n"] += 1
        # 第 2~11 次请求失败（凑满连续 10 次触发重连），之后恢复
        if 2 <= call_state["n"] <= 11:
            return _FakeResult([], error_code="10002007", error_msg="网络接收错误。")
        return _FakeResult([[kw["start_date"], "10", "11", "9", "10.5", "1000", "10500"]])

    with patch("baostock.login", side_effect=fake_login), \
         patch("baostock.logout"), \
         patch("baostock.query_trade_dates",
               return_value=_FakeResult([["2024-01-03", "1"]])), \
         patch("baostock.query_history_k_data_plus", side_effect=fake_k):
        written = engine.sync_today_bulk()

    # 首次登录 + 至少一次重连
    assert len(logins) >= 2
    # 重连后恢复拉取：最后一批股票被写入
    assert written >= 3


# 红线：整轮回填同样只登录一次，且用一次日历查询定终点
def test_backfill_uses_single_login(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "backfill.db")
    today = date.today().isoformat()
    logins: list[int] = []
    k_calls: list[dict] = []

    def fake_login(*args, **kwargs):
        logins.append(1)
        return _FakeResult([], error_code="0")

    def fake_k(**kwargs):
        k_calls.append(kwargs)
        return _FakeResult(
            [[kwargs["end_date"], "10", "11", "9", "10.5", "1000", "10500"]],
            fields=["date", "open", "high", "low", "close", "volume", "amount"],
        )

    with patch("baostock.login", side_effect=fake_login), \
         patch("baostock.logout"), \
         patch("baostock.query_trade_dates",
               side_effect=lambda **kw: _FakeResult([[today, "1"]])) as cal_mock, \
         patch("baostock.query_history_k_data_plus", side_effect=fake_k):
        engine.backfill(["000001", "600000", "300750"])

    assert len(logins) == 1
    assert cal_mock.called is True
    assert len(k_calls) == 3
    assert {c["end_date"] for c in k_calls} == {today}
    with sqlite3.connect(engine.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0] == 3


# baostock 不可用时不再有 akshare 兜底：返回 0，不写入异源口径数据
def test_sync_today_bulk_returns_zero_when_baostock_down(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "bulk_bs_down.db")
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute(
            "INSERT INTO stock_daily (symbol, date, close, volume)"
            " VALUES ('000001', '2024-01-02', 10.0, 100.0)"
        )
        conn.commit()

    with patch("sequoia_x.data.engine.bs_login", return_value=False), \
         patch("time.sleep"):
        assert engine.sync_today_bulk() == 0


# Property: 登录预算封顶，持续失败时不会无限新建连接
def test_bs_login_respects_round_budget() -> None:
    calls: list[int] = []

    def fake_login():
        calls.append(1)
        return _FakeResult([], error_code="10002007", error_msg="网络接收错误。")

    # 关掉磁盘熔断，单独验证"轮内预算"这道闸 —— 否则第一次失败就被熔断拦住，测不到预算
    with patch("baostock.login", side_effect=fake_login), \
         patch("time.sleep"), \
         patch.object(engine_module.bs_breaker, "cooldown_remaining", return_value=0.0), \
         patch.object(engine_module.bs_breaker, "record_failure", return_value={}):
        for _ in range(20):
            assert engine_module.bs_login() is False

    assert len(calls) == engine_module._LOGIN_BUDGET


# 红线：网络层不可达时服务端收不到请求，重试只是白等一个超时
def test_bs_login_does_not_retry_when_network_down() -> None:
    calls: list[int] = []

    def fake_login():
        calls.append(1)
        return _FakeResult([], error_code="10002007", error_msg="网络接收错误。")

    with patch("baostock.login", side_effect=fake_login), patch("time.sleep"):
        assert engine_module.bs_login() is False

    assert len(calls) == 1


# 红线：熔断期内一个 socket 都不建 —— 封禁期间的重试只会刷新服务端计时
def test_bs_login_short_circuits_during_cooldown() -> None:
    engine_module.bs_breaker.record_failure("连接超时")
    calls: list[int] = []

    with patch("baostock.login", side_effect=lambda *a, **k: calls.append(1)):
        assert engine_module.bs_login() is False

    assert calls == []


# 红线：登录失败必须落盘，下一轮跑批（新进程）不该再去撞同一堵墙
def test_bs_login_persists_failure_across_processes() -> None:
    def fake_login():
        return _FakeResult([], error_code="10002007", error_msg="网络接收错误。")

    with patch("baostock.login", side_effect=fake_login), patch("time.sleep"):
        assert engine_module.bs_login() is False

    assert engine_module.bs_breaker.cooldown_remaining() > 0


# Property: 账号级拒绝（10001011 黑名单）一次就收手，后续调用不再发起连接
def test_bs_login_stops_immediately_when_blacklisted() -> None:
    calls: list[int] = []

    def fake_login():
        calls.append(1)
        return _FakeResult([], error_code="10001011", error_msg="黑名单用户，请与管理员联系")

    with patch("baostock.login", side_effect=fake_login), patch("time.sleep"):
        assert engine_module.bs_login() is False
        assert engine_module.bs_login() is False

    assert len(calls) == 1


# 红线：连续失败达阈值就收手，不再把剩余代码全敲一遍
def test_backfill_breaks_after_consecutive_failures(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "backfill_break.db")
    today = date.today().isoformat()
    logins: list[int] = []
    k_calls: list[dict] = []

    def fake_login(*args, **kwargs):
        logins.append(1)
        return _FakeResult([], error_code="0")

    def fake_k(**kwargs):
        k_calls.append(kwargs)
        return _FakeResult([], error_code="10001004", error_msg="数据不存在")

    with patch("baostock.login", side_effect=fake_login), \
         patch("baostock.logout"), \
         patch("baostock.query_trade_dates", side_effect=lambda **kw: _FakeResult([[today, "1"]])), \
         patch("baostock.query_history_k_data_plus", side_effect=fake_k), \
         patch("time.sleep"):
        engine.backfill([f"{i:06d}" for i in range(1, 31)])

    # 熔断在第 10 只，每只 2 次尝试；登录仍只有开场那一次
    assert len(logins) == 1
    assert len(k_calls) == 20
