"""数据引擎属性测试。"""

import gc
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


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
         patch("sequoia_x.data.engine._bs_login", return_value=True):
        symbols = engine.sync_stock_basic()

    assert symbols == ["600000", "000001"]
    assert engine.get_stock_names(symbols) == {
        "600000": "浦发银行",
        "000001": "平安银行",
    }
