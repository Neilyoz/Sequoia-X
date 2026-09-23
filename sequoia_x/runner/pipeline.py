"""跑批编排：run_daily / run_backfill，CLI 与服务端共用的唯一一条业务链路。

本层禁止退出码调用、标准输出直打与 get_settings()：退出码是 CLI 的职责，配置由
调用方注入（约束 01-constraints.md §2）。日志文案是用户判断跑批状态的依据，
改动等于改 UI，勿动。
"""

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import baostock_breaker as bs_breaker
from sequoia_x.data.engine import DataEngine, bs_reset_login_budget
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.runner.registry import resolve


@dataclass
class StrategyOutcome:
    """单个策略一次跑批的结果。

    error 非 None 表示该策略抛了异常——不终止整批，只跳过自己（与原 main.py
    "一挂全挂"的刻意差异，见 T1 任务文档 §3.3 不变量）。
    """

    strategy_name: str
    webhook_key: str
    symbols: list[str]
    pushed: bool
    error: str | None = None


@dataclass
class DailyReport:
    """日常跑批的完整报告。to_dict() 供 T2 落库为 TaskRecord.result。"""

    trade_date: str
    synced_rows: int
    outcomes: list[StrategyOutcome]
    push_enabled: bool

    @property
    def total_signals(self) -> int:
        """全部策略选股数量之和。"""
        return sum(len(o.symbols) for o in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化视图，字段名与 dataclass 一致，outcomes 展开为 dict 列表。"""
        return asdict(self)


def run_daily(
    settings: Settings,
    *,
    push: bool = True,
    strategies: list[str] | None = None,
) -> DailyReport:
    """日常跑批：同步今日快照 → 逐策略选股 → 有结果则推送飞书。

    strategies 为 None 表示注册表全部；push=False 只跑不推（API 调试用）。
    行情同步固定单进程单连接（见 DataEngine.sync_today_bulk）：baostock 的黑名单
    按新建连接频率触发，并行 worker 的多路 anonymous 登录是主要风险源。
    """
    logger = get_logger(__name__)
    # 登录预算按轮计：常驻服务进程里跑几十天后，累积值会先把本轮会话饿死。
    bs_reset_login_budget()
    # 每次跑批自建 engine：其内部每个操作独立 connect，跨任务复用无收益反有脏连接风险。
    engine = DataEngine(settings)

    # 名称走 baostock（唯一数据源，见 sync_stock_basic）。
    # return_local_on_failure=False：日常不该把历史代码当当日清单，拿不到就空手过。
    # 失败只记日志不抛，缺名称时飞书卡片退回显示雪球代码。
    engine.sync_stock_basic(return_local_on_failure=False)

    logger.info("开始拉取最新快照...")
    count = engine.sync_today_bulk()
    logger.info(f"快照同步完成，写入 {count} 只股票")

    if count == 0:
        # 非交易日保护（T4 §4.4）：旧数据上重跑会选出与昨天相同的结果并再次推送
        # 飞书（约束 §6 的骚扰场景），cron 的 1-5 排除不了法定节假日，在这里兜底。
        # 刻意行为变更：CLI 在休市日同样打印本条 WARNING 后快速结束。
        logger.warning("今日无新增行情（可能非交易日），跳过策略执行")
        return DailyReport(
            trade_date=date.today().strftime("%Y-%m-%d"),
            synced_rows=0,
            outcomes=[],
            push_enabled=push,
        )

    outcomes: list[StrategyOutcome] = []
    notifier = FeishuNotifier(settings, engine)
    for cls in resolve(strategies):
        strategy = cls(engine=engine, settings=settings)
        name = cls.__name__
        logger.info(f"执行策略：{name}")
        try:
            selected: list[str] = strategy.run()
        except Exception as exc:
            logger.exception(f"{name} 执行异常，跳过该策略")
            outcomes.append(
                StrategyOutcome(
                    strategy_name=name,
                    webhook_key=strategy.webhook_key,
                    symbols=[],
                    pushed=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        logger.info(f"{name} 选出 {len(selected)} 只股票")
        pushed = False
        if selected and push:
            notifier.send(
                symbols=selected,
                strategy_name=name,
                webhook_key=strategy.webhook_key,
            )
            pushed = True
        elif selected:
            logger.info(f"{name} 有选股结果，但 push 已关闭，跳过推送")
        else:
            logger.info(f"{name} 无选股结果，跳过推送")
        outcomes.append(
            StrategyOutcome(
                strategy_name=name,
                webhook_key=strategy.webhook_key,
                symbols=selected,
                pushed=pushed,
            )
        )

    return DailyReport(
        trade_date=date.today().strftime("%Y-%m-%d"),
        synced_rows=count,
        outcomes=outcomes,
        push_enabled=push,
    )


def run_backfill(settings: Settings) -> dict[str, Any]:
    """回填模式：同步全市场证券列表 → baostock 拉历史 K 线。

    回填的多轮重跑与重试细节留在 engine 层打日志，这里只汇报清单规模。
    """
    logger = get_logger(__name__)
    bs_reset_login_budget()
    engine = DataEngine(settings)
    logger.info("进入回填模式...")
    all_symbols = engine.sync_stock_basic()
    engine.backfill(all_symbols)
    logger.info("Sequoia-X V2 回填模式运行完成")
    return {"symbols_synced": len(all_symbols)}


def run_sync_names(settings: Settings) -> dict[str, Any]:
    """只同步股票名称：唯一数据源 baostock。"""
    logger = get_logger(__name__)
    engine = DataEngine(settings)
    codes = engine.sync_stock_basic(return_local_on_failure=False)
    logger.info(f"股票名称同步完成，清单 {len(codes)} 只")
    return {"names_synced": len(codes)}


def run_reset_baostock() -> dict[str, Any]:
    """清除 baostock 熔断状态。

    服务端封禁是按源 IP 记的，换出口 IP（拨号重连、切网络、走代理）就绕开了；
    不清状态的话新一轮跑批会被自己的冷却窗口挡在门外。
    """
    logger = get_logger(__name__)
    previous = bs_breaker.load()
    bs_breaker.reset()
    if previous:
        logger.info(
            f"已清除 baostock 熔断状态（此前连续失败 {previous.get('failures')} 次，"
            f"原因：{previous.get('reason', '未知')}）"
        )
    else:
        logger.info("baostock 熔断状态为空，无需清除")
    return {"cleared": bool(previous)}
