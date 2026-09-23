"""启动期副作用收口的测试：幂等性与 socket 超时值。"""

import socket
from unittest.mock import patch

from sequoia_x.core import bootstrap as bootstrap_module


def test_bootstrap_repeated_calls_are_idempotent() -> None:
    """性质（约束 §1）：bootstrap() 可被 CLI 与 API 各自调用，重复调用不抛异常。"""
    bootstrap_module.bootstrap()
    bootstrap_module.bootstrap()


def test_bootstrap_sets_socket_timeout_60() -> None:
    """全局 socket 必须是 60 秒：baostock 全市场证券列表单次 recv 要 60~72 秒，
    历史教训是设 10 秒会把接口"掐死成超时"（原 main.py 注释迁移至此）。"""
    bootstrap_module.bootstrap()
    assert socket.getdefaulttimeout() == 60.0


def test_bootstrap_does_not_reload_dotenv_on_second_call() -> None:
    """幂等的含义是"无副作用"：第二次调用不得再执行 load_dotenv。"""
    bootstrap_module.bootstrap()
    with patch.object(bootstrap_module, "load_dotenv") as mock_load:
        bootstrap_module.bootstrap()
    mock_load.assert_not_called()
