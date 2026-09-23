"""启动期副作用统一收口。

必须在 import 任何 sequoia_x 业务子模块之前调用，原因见
docs/fastapi-service/01-constraints.md §1：uvicorn 导入 app 的路径与 CLI 完全不同，
副作用不收口就会出现"CLI 能跑、API 跑不了"的分裂故障。
"""

import socket

from dotenv import load_dotenv

_bootstrapped: bool = False


def bootstrap() -> None:
    """统一执行启动期副作用：加载 .env、设置全局 socket 超时。

    幂等，重复调用无副作用。CLI 入口与 API 入口都必须在 import 任何
    sequoia_x 业务子模块之前调用它。本函数内部不得 import sequoia_x.core.config，
    否则调用方无法保证"先 bootstrap 再实例化 Settings"的顺序。
    """
    global _bootstrapped
    if _bootstrapped:
        return
    # load_dotenv 找不到 .env 时静默返回，与改造前 main.py 的行为一致。
    load_dotenv()
    # 全局兜底超时，主要给 akshare 用（它不接受 timeout 参数）。
    # 不能设太小：baostock 走裸 socket，会继承这个"单次 recv"超时，而它的全市场
    # 证券列表单次要 60~72 秒、分块间隔实测可达 4 秒，10 秒会把它掐死成"接口超时"。
    socket.setdefaulttimeout(60.0)
    _bootstrapped = True
