"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import baostock_guard as bs_guard

logger = get_logger(__name__)

bs_guard.install()


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""

_CREATE_STOCK_BASIC_SQL = """
CREATE TABLE IF NOT EXISTS stock_basic (
    symbol     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _bs_login(max_retries: int = 4) -> bool:
    """登录 baostock，失败按 3/6/12s 退避重试，每次重试前先丢弃旧连接。

    服务端会瞬时掐断长连接（表现为解码错或"网络接收错误"），一次失败不代表
    不可用，所以登录本身必须重试 —— 否则整轮回填会被一次抖动直接终止。
    但账号侧的拒绝（黑名单、登录数上限）重试无用，继续重连只会加重限流，直接放弃。
    """
    import time

    import baostock as bs
    import baostock.common.contants as cons

    fatal = {
        cons.BSERR_BLACKLIST_USER,
        cons.BSERR_LOGIN_COUNT_LIMIT,
        cons.BSERR_ACCESS_INSUFFICIENCE,
        cons.BSERR_CLIENT_VESION_EXPIRE,
    }

    for attempt in range(max_retries):
        bs_guard.drop_connection()
        try:
            lg = bs.login()
        except Exception as exc:
            reason = str(exc)
        else:
            if lg.error_code == "0":
                return True
            reason = lg.error_msg
            if lg.error_code in fatal:
                logger.error(
                    f"baostock 拒绝登录({lg.error_code}: {reason})，"
                    "这是服务端对本 IP/anonymous 账号的限流，重试只会加重，终止本轮"
                )
                return False

        logger.warning(f"baostock 登录失败({attempt + 1}/{max_retries}): {reason}")
        if attempt < max_retries - 1:
            time.sleep(3 * 2 ** attempt)
    return False


def _bs_query_k(bs_code: str, start: str, end: str):
    """查询单只股票日 K；会话被服务端作废时静默重登后立刻重试一次。

    anonymous 会话会被 baostock 服务端提前作废，表现为 socket 还活着、
    响应却是 10001001 用户未登陆。这不是网络故障，重登即可恢复，
    不该占用退避重试的次数、也不该打 warning。
    每只股票最多重登一次：`bs.login()` 每次都新建一条 TCP 连接，
    密集重连是 baostock 反滥用（10001011 黑名单）的触发条件。
    """
    import baostock as bs
    import baostock.common.contants as cons

    def query():
        return bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",  # 后复权
        )

    rs = query()
    if rs.error_code != cons.BSERR_NO_LOGIN:
        return rs
    return query() if _bs_login(max_retries=1) else rs


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。"""
    import baostock as bs

    if not _bs_login():
        logger.error("worker 无法登录 baostock，本批次跳过")
        return []
    results = []
    for symbol, bs_code, start, end in tasks:
        rs = _bs_query_k(bs_code, start, end)
        if rs.error_code != "0":
            continue
        while rs.next():
            results.append([symbol] + rs.get_row_data())
    bs.logout()
    return results


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_STOCK_BASIC_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        n_workers = min(8, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(
            all_rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql(
                "stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500
            )
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 登录失败按 3/6/12s 退避重试（服务端会瞬时掐断连接）
        - 会话被服务端作废（用户未登陆）时静默重登，不占用下面的重试次数
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 连续 10 只失败即停止（服务端限流/黑名单时继续重连只会加重）
        - 已入库的自动 skip，中断后可重跑续传
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        if not _bs_login():
            logger.error("baostock 连续登录失败，回填终止")
            return

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0
        consecutive_failed = 0
        max_consecutive_failed = 10  # 连续 N 只失败即判定被限流，停止而不是继续冲击

        try:
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs_guard.drop_connection()
                    time.sleep(1)
                    if not _bs_login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = _bs_query_k(bs_code, start, today_str)

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt == max_retries - 1:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败: {exc}，跳过")
                            break
                        wait = 2 ** (attempt + 1)
                        logger.warning(
                            f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                        )
                        time.sleep(wait)
                        # 丢弃被污染的连接后重登（不能在被污染的连接上 logout）
                        bs_guard.drop_connection()
                        time.sleep(1)
                        if not _bs_login():
                            logger.error(f"[{symbol}] 重登失败，放弃本只")
                            break

                if not query_ok:
                    failed += 1
                    consecutive_failed += 1
                    if consecutive_failed >= max_consecutive_failed:
                        logger.error(
                            f"连续 {consecutive_failed} 只拉取失败，判定 baostock 已对本 IP 限流，"
                            f"终止回填（继续重连只会加重）。已入库数据可续传，请稍后重跑"
                        )
                        break
                    continue

                consecutive_failed = 0

                if not rows:
                    skipped += 1
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily", conn, if_exists="append",
                            index=False, method="multi", chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表与名称 ──

    def sync_stock_basic(self) -> list[str]:
        """拉取全市场 A 股代码与名称，写入 stock_basic 表并返回代码列表。

        该接口单次返回约 52 万字节，实测耗时 60~72 秒，是全流程最脆弱的一步。
        失败时重登重试；仍失败则回退到本地已入库代码，避免回填在第一
        步就被一次网络抖动直接终止。
        """
        import time

        import baostock as bs

        max_attempts = 3
        for attempt in range(max_attempts):
            if not _bs_login():
                logger.warning(f"同步股票列表失败（第{attempt + 1}/{max_attempts}次）：无法登录")
                continue
            try:
                rs = bs.query_stock_basic(code_name="", code="")
                if rs.error_code != "0":
                    raise RuntimeError(rs.error_msg)

                items: list[tuple[str, str]] = []
                while rs.next():
                    row = rs.get_row_data()
                    code = row[0]           # "sh.600000" or "sz.000001"
                    name = row[1]           # code_name
                    status = row[4]         # "1" = 上市
                    stock_type = row[5]     # "1" = 股票
                    if status == "1" and stock_type == "1":
                        items.append((code.split(".")[1], name))  # 提取纯数字代码
                if not items:
                    raise RuntimeError("返回结果为空")

                saved = self._save_stock_basic(items)
                logger.info(f"股票列表同步完成，共 {len(items)} 只，写入名称 {saved} 条")
                bs.logout()
                return [code for code, _ in items]
            except Exception as exc:
                logger.warning(f"同步股票列表异常（第{attempt + 1}/{max_attempts}次）: {exc}")
                bs_guard.drop_connection()
                time.sleep(3 * 2 ** attempt)

        local = self.get_local_symbols()
        if local:
            logger.warning(f"远端股票列表不可用，回退到本地已入库代码 {len(local)} 只")
            return local
        logger.error("无法获取股票列表：远端拉取失败且本地无历史数据")
        return []

    def _save_stock_basic(self, items: list[tuple[str, str]]) -> int:
        """写入/覆盖 (symbol, name)，名称会变（如 ST 更名）所以每次同步都刷新。"""
        from datetime import date

        today = date.today().isoformat()
        rows = [(code, name, today) for code, name in items if name]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO stock_basic (symbol, name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, "
                "updated_at = excluded.updated_at",
                rows,
            )
            conn.commit()
        return len(rows)

    def get_stock_names(self, symbols: list[str]) -> dict[str, str]:
        """从本地 stock_basic 表读取股票名称；未收录的代码不出现在结果中。"""
        if not symbols:
            return {}
        placeholders = ",".join("?" * len(symbols))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT symbol, name FROM stock_basic WHERE symbol IN ({placeholders})",
                list(symbols),
            ).fetchall()
        return dict(rows)

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
