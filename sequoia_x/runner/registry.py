"""策略注册表：策略清单与名称解析的单点维护。

取代原 main.py 里的硬编码策略列表：新增策略只需在 STRATEGY_CLASSES 追加一行。
注册表顺序即跑批顺序，调整顺序会改变日志与结果序列，测试对此有逐一断言。
"""

from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy


class UnknownStrategyError(Exception):
    """resolve() 收到注册表之外的名称。消息里列出全部合法别名便于排查。"""


def _camel_to_snake(name: str) -> str:
    """MaVolumeStrategy -> ma_volume_strategy，提供下划线形式的类名别名。"""
    parts: list[str] = []
    for ch in name:
        if ch.isupper() and parts:
            parts.append("_")
        parts.append(ch.lower())
    return "".join(parts)


# 顺序 = 原 main.py 策略列表顺序（架构 §3.2 的跨任务契约，不得重排）。
STRATEGY_CLASSES: tuple[type[BaseStrategy], ...] = (
    MaVolumeStrategy,
    TurtleTradeStrategy,
    HighTightFlagStrategy,
    LimitUpShakeoutStrategy,
    UptrendLimitDownStrategy,
    RpsBreakoutStrategy,
    PrivatePlacementStrategy,
)

# 每类策略登记三种合法别名：精确类名、下划线小写类名、webhook_key。
# 刻意不收录"类名纯小写"（如 mavolumestrategy），任务文档 T1 §4.2 只要求前两种形式。
STRATEGY_KEYS: dict[str, type[BaseStrategy]] = {
    alias: cls
    for cls in STRATEGY_CLASSES
    for alias in (cls.__name__, _camel_to_snake(cls.__name__), cls.webhook_key.lower())
}


def get_all_strategies() -> list[type[BaseStrategy]]:
    """返回注册表顺序的全部策略类。

    返回新列表而非内部 tuple 的引用，防止调用方改动污染注册表。
    """
    return list(STRATEGY_CLASSES)


def resolve(names: list[str] | None) -> list[type[BaseStrategy]]:
    """按类名或 webhook_key 解析策略子集。

    names 为 None 表示全部；输出保持注册表原始顺序而非传入顺序，保证跑批顺序
    稳定、结果可与历史对比；同一策略的多种别名会去重；含未知名称抛
    UnknownStrategyError。
    """
    if names is None:
        return get_all_strategies()
    matched: set[type[BaseStrategy]] = set()
    for name in names:
        cls = STRATEGY_KEYS.get(name) or STRATEGY_KEYS.get(name.lower())
        if cls is None:
            valid = "、".join(sorted(STRATEGY_KEYS))
            raise UnknownStrategyError(f"未知的策略名称：{name}。合法名称：{valid}")
        matched.add(cls)
    return [cls for cls in STRATEGY_CLASSES if cls in matched]
