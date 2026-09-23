"""日志模块：基于 rich 库提供带颜色的结构化终端日志输出。"""

import logging

from rich.logging import RichHandler

_FORMAT = "%(name)s - %(message)s"

# 额外 handler 注册表：任务日志归集（sequoia_x/task/logs.py）需要在 get_logger
# 惰性新建 logger 时补挂捕获 handler——业务 logger 各自 propagate=False，
# 挂在 "sequoia_x" 父 logger 上是收不到的，只能逐个叶子挂载。
_extra_handlers: list[logging.Handler] = []


def register_handler(handler: logging.Handler) -> None:
    """注册"额外 handler"：此后 get_logger 新建的每个 logger 都会附带它。

    不影响已存在的 logger——那部分由调用方自行遍历挂载（见 task/logs.py）。
    """
    if handler not in _extra_handlers:
        _extra_handlers.append(handler)


def get_logger(name: str) -> logging.Logger:
    """
    工厂函数，返回配置了 RichHandler 的 Logger 实例。

    支持 DEBUG/INFO/WARNING/ERROR 四级日志，由 rich 自动以不同颜色渲染。
    每条日志包含时间戳、模块名和日志级别。
    同名 logger 不重复添加 handler（幂等性）。

    Args:
        name: logger 名称，通常传入 __name__。

    Returns:
        logging.Logger: 配置好的 Logger 实例。
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    handler = RichHandler(
        rich_tracebacks=True,
        show_path=False,
        log_time_format="[%Y-%m-%d %H:%M:%S]",
    )
    handler.setFormatter(logging.Formatter(_FORMAT))

    logger.addHandler(handler)
    for extra in _extra_handlers:
        logger.addHandler(extra)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    return logger
