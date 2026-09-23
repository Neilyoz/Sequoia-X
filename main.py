"""Sequoia-X V2 主程序入口（CLI）。

运行模式：
  python main.py                  # 日常模式：单进程串行增量补数据 + 跑策略 + 飞书推送
  python main.py --backfill       # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）
  python main.py --names          # 只同步股票名称（数据源：baostock）
  python main.py --reset-baostock # 清除 baostock 熔断状态（换出口 IP 后用，立刻恢复连通）
服务化入口见 sequoia_x/api/app.py，两者共用 sequoia_x/runner 编排层。
"""

import argparse
import sys

from sequoia_x.core.bootstrap import bootstrap

# bootstrap() 必须先于一切 sequoia_x 业务子模块 import（约束 §1：.env 要先于 Settings
# 实例化、socket 超时要先于 baostock/akshare 建连），故下方 import 刻意在语句之后。
bootstrap()

# get_settings 这个名字必须留在 main 模块命名空间且以模块级 import 形式存在：
# tests/test_main.py 的契约就是 patch main.get_settings（约束 §2.2）。
from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.runner import pipeline  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    parser.add_argument(
        "--names",
        action="store_true",
        help="只同步股票名称（数据源：baostock）后退出",
    )
    parser.add_argument(
        "--reset-baostock",
        action="store_true",
        help="清除 baostock 熔断状态（换出口 IP 后用，立刻恢复连通）",
    )
    args = parser.parse_args()

    # 显式初始化：收尾日志若因控制流变动移出 try，未定义引用会直接炸。
    logger = None
    try:
        settings = get_settings()
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        if args.reset_baostock:
            pipeline.run_reset_baostock()
            return

        if args.names:
            pipeline.run_sync_names(settings)
            return

        if args.backfill:
            pipeline.run_backfill(settings)
            return

        report = pipeline.run_daily(settings)
        logger.info(f"Sequoia-X V2 运行完成，共选出 {report.total_signals} 个信号")
    except Exception:
        # 双重兜底不可简化：异常本身可能发生在配置/日志初始化链路上，
        # get_logger 也可能连带失败，届时退回原始 traceback 输出。
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
