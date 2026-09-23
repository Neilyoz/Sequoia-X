"""定增公告监控策略：推送最近发布的定向增发公告。

【已停用】原数据源为 akshare stock_qbzf_em()（东方财富-全部增发）。项目已收敛为
baostock 单一数据源，而 baostock 不提供增发公告接口，故本策略暂时停用：run()
恒返回空列表，不产生任何选股结果与推送。待找到 baostock 可覆盖的替代数据源后再恢复。
"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class PrivatePlacementStrategy(BaseStrategy):
    """定增公告监控策略（已停用）。

    停用原因：依赖 akshare，与"baostock 单数据源"的项目约束冲突。
    恢复路径：接入 baostock 可覆盖的公告数据源后，在 run() 中重建筛选逻辑。

    Attributes:
        webhook_key: 路由到 'private_placement' 飞书机器人。
    """

    webhook_key: str = "private_placement"

    def run(self) -> list[str]:
        """恒返回空列表（策略已停用）。"""
        logger.warning("PrivatePlacementStrategy 已停用：依赖 akshare，与 baostock 单数据源约束冲突")
        return []
