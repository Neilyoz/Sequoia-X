"""baostock 加固补丁测试：用假 socket 验证协议行为，全程不联网。"""

import socket

import baostock.common.contants as cons
import baostock.common.context as bs_context
import pytest

from sequoia_x.data import baostock_guard as guard

_TERMINATOR = b"<![CDATA[]]>\n"


class _FakeSock:
    """最小 socket 替身：记录 settimeout 调用，recv 按脚本回放。"""

    def __init__(self, chunks: list[bytes], default_timeout: float = 60.0) -> None:
        self._chunks = chunks
        self.timeouts: list[float | None] = []
        self._default_timeout = default_timeout
        self.closed = False

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)
        self._default_timeout = value

    def gettimeout(self) -> float | None:
        return self._default_timeout

    def send(self, data: bytes) -> None:
        pass

    def recv(self, bufsize: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


def _msg(msg_type: str, body: str) -> str:
    return f"00.9.10{cons.MESSAGE_SPLIT}{msg_type}{cons.MESSAGE_SPLIT}{len(body):010d}" \
           f"{cons.MESSAGE_SPLIT}{body}"


def _reply(msg_type: str, body: str) -> bytes:
    return (_msg(msg_type, body) + _TERMINATOR.decode()).encode("utf-8")


def _run(sock: _FakeSock, msg: str):
    bs_context.default_socket = sock
    try:
        return guard._send_msg(msg)
    finally:
        guard.drop_connection()


def test_login_request_uses_short_timeout_then_restores() -> None:
    """登录握手单独用短超时，返回后恢复原值，不污染后续大响应请求。"""
    sock = _FakeSock([_reply(cons.MESSAGE_TYPE_LOGIN_RESPONSE, "0\x01success")])
    _run(sock, _msg(cons.MESSAGE_TYPE_LOGIN_REQUEST, "login\x01anonymous"))
    assert sock.timeouts == [guard._LOGIN_TIMEOUT, 60.0]


def test_non_login_request_keeps_default_timeout() -> None:
    sock = _FakeSock([_reply(cons.MESSAGE_TYPE_GETKDATAPLUS_REQUEST, "0\x01success")])
    _run(sock, _msg(cons.MESSAGE_TYPE_GETKDATAPLUS_REQUEST, "query_history_k_data_plus"))
    assert sock.timeouts == []


def test_peer_closed_returns_immediately_without_spinning() -> None:
    """对端关闭时 recv 返回空：立刻报错并丢弃连接，而不是原版的死循环空转。"""
    sock = _FakeSock([])
    assert _run(sock, _msg(cons.MESSAGE_TYPE_GETKDATAPLUS_REQUEST, "x")) is None
    assert sock.closed is True


def test_short_connect_timeout_restores_previous_value() -> None:
    """登录期收紧全局超时，出门恢复：全市场查询仍依赖 bootstrap 设的 60s。"""
    original = socket.getdefaulttimeout()
    with guard.short_connect_timeout(3.0):
        assert socket.getdefaulttimeout() == 3.0
    assert socket.getdefaulttimeout() == original


def test_short_connect_timeout_restores_on_exception() -> None:
    """登录抛异常也必须恢复，否则一次失败会把全局超时永久改小。"""
    original = socket.getdefaulttimeout()
    with pytest.raises(ValueError):
        with guard.short_connect_timeout(3.0):
            raise ValueError("boom")
    assert socket.getdefaulttimeout() == original
