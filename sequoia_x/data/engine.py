"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import baostock.common.contants as cons
import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import baostock_breaker as bs_breaker
from sequoia_x.data import baostock_guard as bs_guard

logger = get_logger(__name__)

bs_guard.install()

# A 股收盘 15:00，日线数据落库有几分钟延迟，留到 15:10 才算「当日已收盘」。
_MARKET_CLOSE_HOUR = 15
_MARKET_CLOSE_MINUTE = 10


def _after_close() -> bool:
    """当前时刻是否已过 A 股当日收盘 + 数据落库延迟。"""
    now = datetime.now()
    return (now.hour, now.minute) >= (_MARKET_CLOSE_HOUR, _MARKET_CLOSE_MINUTE)



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


# baostock 的反滥用盯的是"新建连接频率"，不是请求量：bs.login() 每次都会
# SocketUtil.connect() 开一条新 TCP（见 baostock/login/loginout.py:63）。加固版把
# 登录做成了 4 次退避重试 × 多个调用点（Pool(8) 每个 worker 一次、每 200 只预防性
# 重连一次、每只股票会话恢复一次），一次跑批能打出上百条连接，正好踩中 10001011
# 黑名单。原始版本每进程只登录一次，所以从不被封。
# 这里按"每轮跑批"给登录数封顶：预算用完就判定服务端有问题、直接停手，而不是
# 继续重连加重限流。预算在 pipeline 入口清零（常驻服务进程跨轮复用模块状态）。
_LOGIN_BUDGET = 10
_logins_used = 0
_login_blocked = False  # 服务端明确拒绝（黑名单/权限）后，本进程不再碰 baostock

# 重试无意义的账号级拒绝：继续冲击只会把限流坐实成黑名单
_FATAL_LOGIN_CODES = frozenset({
    cons.BSERR_BLACKLIST_USER,        # 10001011 黑名单用户
    cons.BSERR_LOGIN_COUNT_LIMIT,     # 10001005 并发登录数超限
    cons.BSERR_ACCESS_INSUFFICIENCE,  # 10001006 权限不足
    cons.BSERR_CLIENT_VESION_EXPIRE,  # 10001004 客户端版本过期
})


def bs_reset_login_budget() -> None:
    """开新一轮跑批：清零登录预算与进程内封禁判定。

    刻意不动磁盘熔断（baostock_breaker）：预算回答"这一轮允许试几次"，熔断回答
    "服务端刚刚是不是确实不可达"，两者语义不同。换出口 IP 后要立刻恢复连通，
    走 `python main.py --reset-baostock`，不是这个函数。
    """
    global _logins_used, _login_blocked
    _logins_used = 0
    _login_blocked = False


def bs_login(max_retries: int = 2) -> bool:
    """登录 baostock，占用的登录次数受本轮登录预算约束。

    只做 2 次尝试、间隔 10s：一次登录失败通常是服务端限流的信号，按秒级退避
    猛敲只会把它从限流升级成黑名单。致命错误（账号/IP 被拒）直接置
    `_login_blocked`，本进程后续调用不再发起任何连接。

    磁盘熔断先于预算判断：上一轮跑批已确认不可达时，这一轮连 socket 都不建。
    登录的 connect 用短超时（见 bs_guard.short_connect_timeout）—— IP 被服务端
    防火墙 DROP 时它本该立刻失败，而不是每次都等满全局 60s。
    """
    global _logins_used, _login_blocked

    import time

    import baostock as bs

    if _login_blocked:
        return False

    remaining = bs_breaker.cooldown_remaining()
    if remaining > 0:
        logger.warning(
            f"baostock 熔断中（剩余 {remaining / 60:.0f} 分钟，上次原因："
            f"{bs_breaker.load().get('reason', '未知')}），本轮不发起任何连接"
        )
        return False

    if _logins_used >= _LOGIN_BUDGET:
        logger.error(
            f"baostock 登录次数已达本轮上限（{_LOGIN_BUDGET}），判定服务端持续不可用，"
            "停止重连（继续冲击会触发黑名单）。已入库数据可续传，请稍后重跑"
        )
        return False

    reason = "登录失败"
    for attempt in range(max_retries):
        if bs_guard.current_socket() is not None:
            # 登录被服务端拒绝时库不会关掉那条 socket，不先关就每试一次漏一个 fd
            bs_guard.drop_connection()
        _logins_used += 1
        network_down = False
        try:
            with bs_guard.short_connect_timeout():
                lg = bs.login()
        except Exception as exc:
            reason = str(exc)
            network_down = isinstance(exc, OSError)
        else:
            if lg.error_code == "0":
                bs_breaker.record_success()
                return True
            reason = f"{lg.error_code}: {lg.error_msg}"
            if lg.error_code in _FATAL_LOGIN_CODES:
                _login_blocked = True
                bs_breaker.record_failure(reason)
                logger.error(
                    f"baostock 拒绝登录({reason})，这是服务端对本 IP/anonymous 账号的限流，"
                    "重试只会加重，本进程后续不再连接"
                )
                return False
            # 网络接收错误 = 包根本没送到服务端（IP 被防火墙 DROP 就是这个形态）
            network_down = lg.error_code == cons.BSERR_RECVSOCK_FAIL

        logger.warning(f"baostock 登录失败({attempt + 1}/{max_retries}): {reason}")
        if network_down:
            # 重试只是再白等一个短超时，服务端连请求都没收到
            logger.warning("baostock 网络层不可达，服务端未收到请求，不再重试")
            break
        if attempt < max_retries - 1:
            time.sleep(10)

    # 尝试都没能登录：写盘冷却，下一轮跑批直接回退本地数据
    bs_breaker.record_failure(reason)
    return False


def bs_logout() -> None:
    """登出并释放连接；失败只记日志，不影响主流程。"""
    import baostock as bs

    if bs_guard.current_socket() is None:
        return
    try:
        bs.logout()
    except Exception as exc:
        logger.warning(f"baostock 登出异常：{exc}")
    finally:
        bs_guard.drop_connection()


class BaostockSession:
    """一条 baostock 连接上的查询，会话失效或连接断开时自愈一次。

    自愈走 `bs_login(max_retries=1)`，受本轮登录预算约束，因此整轮跑批新增的连接数
    只有个位数上界；恢复仍失败时把原始错误返回给调用方，由熔断逻辑收尾。

    需要兜住的两种失败：
    - 10001001 用户未登陆：socket 还活着，但服务端已作废物化会话；
    - 10002007 网络接收错误：连接已被 baostock_guard 丢弃，后续请求只会立刻失败。
    """

    _RECOVERABLE = (cons.BSERR_NO_LOGIN, cons.BSERR_RECVSOCK_FAIL)

    def __init__(self) -> None:
        self.usable = False

    def __enter__(self) -> "BaostockSession":
        self.usable = bs_login()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.usable:
            bs_logout()
        return False

    def call(self, func_name: str, **kwargs):
        """按名字调用 baostock 查询接口（需要已登录的会话）。

        `reconnect=False` 时不重连（见 query_k 的说明），失败原样返回。
        """
        import baostock as bs

        func = getattr(bs, func_name)
        rs = func(**kwargs)
        if rs.error_code in self._RECOVERABLE and bs_login(max_retries=1):
            rs = func(**kwargs)
        return rs

    def query_k(self, bs_code: str, start: str, end: str):
        """单只股票日 K（后复权）。

        不自动重连：`query_history_k_data_plus` 内部把 send_msg 的 None 转成
        10002007 错误码返回（history.py:105），看起来像"会话失效"，但逐只拉全市场
        时一次网络抖动就会让成百上千只都返回该码 —— 每只都重连等于把"按连接频率
        封禁"的坑重新挖开。这里失败就失败，交给调用方跳过、下一轮续传。
        """
        import baostock as bs

        return bs.query_history_k_data_plus(
            code=bs_code,
            fields="date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",
        )

    def last_trade_day(self, start: str, end: str, *, closed_only: bool = False) -> str | None:
        """返回 [start, end] 内最后一个交易日，一次查询替代逐只试探。

        三种结果：
        - 正常：区间内的最大交易日；
        - None：区间内没有交易日（休市），调用方应跳过本轮而不是空跑几千次查询；
        - 查询失败：按 `end` 返回，宁可多跑也不因日历抖动漏数据。

        `closed_only=True` 时排除「今天」这个当日（除非已过收盘时刻）：当天 15:00 前
        跑批，baostock 对当日只会返回空行。若把它当目标日，查询区间会退化为
        `已入库日+1 ~ 今天`，而实际能取到的只有到前一日为止 —— 目标是 9-23 却报
        "更新到 9-24"，且当前一日数据也因当日为空而整批丢弃。取上一个已收盘交易日
        才能拿到真正有数据的收盘价。
        """
        try:
            rs = self.call("query_trade_dates", start_date=start, end_date=end)
        except Exception as exc:
            logger.warning(f"交易日历查询异常：{exc}，按 {end} 处理")
            return end
        if rs.error_code != "0":
            logger.warning(f"交易日历查询失败({rs.error_msg})，按 {end} 处理")
            return end

        days: list[str] = []
        while rs.next():
            row = rs.get_row_data()  # [calendar_date, is_trading_day]
            if row[1] == "1":
                days.append(row[0])
        if not days:
            return None

        target = max(days)
        if closed_only and target == date.today().strftime("%Y-%m-%d") and not _after_close():
            # 今天尚未收盘：退回上一个交易日，避免把当日空数据当目标
            prior = [d for d in days if d < target]
            if prior:
                target = max(prior)
        return target


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

    def get_ohlcv_range(self, symbol: str, *, start: str | None = None,
                        end: str | None = None, limit: int = 250) -> pd.DataFrame:
        """按日期区间倒序取至多 limit 条日线（只读，不触发任何网络同步）。

        查询接口专用（T5 §4.2）：倒序取是为了 limit 命中最近的交易日，
        返回前重排为升序（响应给人看图用）。limit 在数据层再 min 一道
        兜底（入口层 pydantic le=500 是第一道，约束 §5 的 LIMIT 硬要求），
        start/end 的格式校验属于入口层职责，数据层不重复做（T5 §4.1）。
        """
        where = ["symbol = ?"]
        args: list[object] = [symbol]
        if start is not None:
            where.append("date >= ?")
            args.append(start)
        if end is not None:
            where.append("date <= ?")
            args.append(end)
        args.append(max(1, min(limit, 500)))
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT date, open, high, low, close, volume, turnover FROM stock_daily"
                f" WHERE {' AND '.join(where)} ORDER BY date DESC LIMIT ?",
                conn,
                params=args,
            )
        return df.sort_values("date", kind="stable").reset_index(drop=True)

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """单进程串行通过 baostock 拉取增量数据（后复权），写入 SQLite。

        原先用 Pool(8) 并行：8 个 worker 各自 login，等于同时对 anonymous 账号开
        8 条连接，正是 `10001005 登录数达到上限` 惩罚的形态。改成串行只占一条
        连接，慢一些换来的是整轮跑批的登录数固定为 1。
        """
        from datetime import date, timedelta

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

        all_rows: list[list] = []
        with BaostockSession() as session:
            if not session.usable:
                # baostock 不可用（限流/黑名单/网络层被封）：没有兜底数据源，本轮不产增量。
                # 数据源已收敛为 baostock 单源，宁可空跑也不写入异源口径的脏数据。
                logger.error("baostock 不可用，本轮无增量（已无 akshare 兜底）")
                return 0

            # 一次日历查询定目标日：休市日直接跳过，省掉 5200 次必然为空的逐只查询。
            # closed_only=True：当天 15:10 前不把「今天」当目标 —— baostock 对未收盘的
            # 当日只返回空行，会让整轮跑批既拉不到当日、又丢掉前一日的收盘数据。
            target = session.last_trade_day(
                min(t[2] for t in tasks), today_str, closed_only=True
            )
            if target is None:
                logger.info(f"{today_str} 前无待补交易日（休市），跳过增量同步")
                return 0
            tasks = [(s, c, st, target) for s, c, st, _ in tasks if st <= target]
            if not tasks:
                logger.info(f"所有股票已更新至 {target}，无需更新")
                return 0

            logger.info(f"需要更新 {len(tasks)} 只股票到 {target}，单进程串行拉取（1 条连接）...")
            failed = 0
            consecutive_failed = 0
            # 连接中途断掉时后续每只都会立刻失败。这里不是直接收工（那会丢掉剩余的
            # 几千只），而是重连一次接着跑：重连受本轮登录预算约束，整轮最多新增
            # 个位数连接，不会把"按连接频率封禁"的坑挖开。
            reconnect_after = 10   # 连续失败到这个数，重连一次
            max_reconnects = 2     # 一轮最多主动重连几次（防服务端持续不可用时刷连接）
            reconnects = 0
            for symbol, bs_code, start, end in tasks:
                try:
                    rs = session.query_k(bs_code, start, end)
                    if rs.error_code == "0":
                        while rs.next():
                            all_rows.append([symbol] + rs.get_row_data())
                        consecutive_failed = 0
                        continue
                    failed += 1
                    consecutive_failed += 1
                except Exception as exc:
                    # 单只异常不拖垮整轮：已取到的数据照常入库，下一轮续传。
                    failed += 1
                    consecutive_failed += 1
                    if failed <= 5:
                        logger.warning(f"[{symbol}] 拉取失败：{exc}")

                if consecutive_failed >= reconnect_after:
                    if reconnects >= max_reconnects:
                        logger.warning(
                            f"已重连 {reconnects} 次仍连续失败，判定 baostock 持续不可用，"
                            f"提前收工（已取 {len(all_rows)} 条，可重跑续传）"
                        )
                        break
                    reconnects += 1
                    if bs_login(max_retries=1):
                        logger.warning(
                            f"连续 {consecutive_failed} 只失败，已重连（第 {reconnects} 次）继续"
                        )
                        consecutive_failed = 0
                    else:
                        logger.warning(
                            f"连续 {consecutive_failed} 只失败且重连失败，提前收工"
                            f"（已取 {len(all_rows)} 条，可重跑续传）"
                        )
                        break

            if failed:
                logger.warning(f"本轮 {failed} 只拉取失败（已跳过，可重跑续传）")

        if not all_rows:
            logger.info("无新数据（可能无成交）")
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

        容错机制（全程只占 1 条连接）：
        - 登录最多 2 次尝试、间隔 10s，且受本轮登录预算约束
        - 会话失效或连接已断时由 BaostockSession 静默重登，不占用下面的重试次数
        - 单只股票失败重试 2 次，间隔 5s（不再自己重连，避免连接数叠加）
        - 连续 10 只失败即停止（服务端限流/黑名单时继续重连只会加重）
        - 已入库的自动 skip，中断后可重跑续传
        """
        import time
        from datetime import date, timedelta

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 2
        retry_wait = 5

        success = 0
        skipped = 0
        failed = 0
        consecutive_failed = 0
        max_consecutive_failed = 10  # 连续 N 只失败即判定被限流，停止而不是继续冲击

        with BaostockSession() as session:
            if not session.usable:
                logger.error("baostock 无法登录，回填终止")
                return

            # 终点钳到最近交易日（一次日历查询），休市日不必为每个代码都跑一次
            # 注定为空的尾巴；日历不可信时退回今天。
            window_start = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
            target_str = session.last_trade_day(window_start, today_str) or today_str

            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= target_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = session.query_k(bs_code, start, target_str)

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
                        logger.warning(
                            f"[{symbol}] 第{attempt + 1}次失败: {exc}，{retry_wait}s 后重试"
                        )
                        time.sleep(retry_wait)

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

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表与名称 ──

    def _fetch_basic_from_baostock(self) -> list[tuple[str, str]] | None:
        """从 baostock 取全市场「上市股票」的 (代码, 名称)；不可用或失败返回 None。

        该接口单次返回约 52 万字节，实测耗时 60~72 秒，是全流程最脆弱的一步。
        只尝试 2 次（每次登录仍受本轮登录预算约束）：这一步失败不值得用重连去换。
        """
        import time

        max_attempts = 2
        for attempt in range(max_attempts):
            with BaostockSession() as session:
                if not session.usable:
                    logger.warning(f"同步股票列表失败（第{attempt + 1}/{max_attempts}次）：无法登录")
                    continue
                try:
                    rs = session.call("query_stock_basic", code_name="", code="")
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
                    return items
                except Exception as exc:
                    logger.warning(f"同步股票列表异常（第{attempt + 1}/{max_attempts}次）: {exc}")
                    time.sleep(3 * 2 ** attempt)
        return None

    def sync_stock_basic(self, *, return_local_on_failure: bool = True) -> list[str]:
        """刷新 stock_basic（代码 + 名称），返回可用于回填的代码清单。

        唯一数据源是 baostock（`query_stock_basic` 一次同时返回代码与名称）。不可用时
        可选回退本地已入库代码（`return_local_on_failure=False` 时不做，用于日常链路
        ——它不该把历史代码当成当日清单）。
        """
        items = self._fetch_basic_from_baostock()
        if items is not None:
            saved = self._save_stock_basic(items)
            logger.info(f"股票列表同步完成，共 {len(items)} 只，写入名称 {saved} 条")
            return [code for code, _ in items]

        if not return_local_on_failure:
            logger.error("baostock 不可用，本轮未刷新股票列表")
            return []

        local = self.get_local_symbols()
        if local:
            logger.warning(f"baostock 不可用，回退到本地已入库代码 {len(local)} 只")
            return local
        logger.error("无法获取股票列表：baostock 拉取失败且本地无历史数据")
        return []

    def sync_stock_names(self) -> tuple[int, list[str]]:
        """同步全市场股票名称到 stock_basic，返回 (写入条数, 代码列表)。

        与 `sync_stock_basic` 同源（baostock query_stock_basic），保留该方法名是为了
        兼容既有调用点。baostock 不可用时返回 (0, [])——不再有 akshare 兜底。
        """
        items = self._fetch_basic_from_baostock()
        if items is None:
            logger.warning("baostock 不可用，股票名称同步跳过")
            return 0, []
        saved = self._save_stock_basic(items)
        logger.info(f"股票名称同步完成，共 {len(items)} 只，写入 {saved} 条")
        return saved, [code for code, name in items if name]

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

    def get_stock_basic_page(
        self, *, keyword: str | None, limit: int, offset: int
    ) -> tuple[list[dict[str, str]], int, bool]:
        """只读查询股票基础列表，按代码或名称匹配关键词并分页。"""
        normalized = (keyword or "").strip()
        escaped = normalized.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped}%"
        db_uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"

        with closing(sqlite3.connect(db_uri, uri=True)) as conn:
            stock_count = conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
            if normalized:
                where = " WHERE symbol LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'"
                params: list[object] = [pattern, pattern]
            else:
                where = ""
                params = []

            total = conn.execute(
                "SELECT COUNT(*) FROM stock_basic" + where, params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT symbol, name FROM stock_basic"
                + where
                + " ORDER BY symbol ASC LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 200)), max(0, offset)],
            ).fetchall()

        items = [{"symbol": row[0], "name": row[1]} for row in rows]
        return items, total, bool(stock_count)

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
