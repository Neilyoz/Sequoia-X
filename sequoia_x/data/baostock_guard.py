"""baostock 客户端加固：修掉库内的死连接空转，并提供干净的断开重连手段。

baostock 0.9.1 的 `util/socketutil.send_msg` 只以结尾的 `<![CDATA[]]>\\n` 作为
收完标志。两种情况会让它失稳：

1. 对端已关闭时 `recv()` 立即返回 `b""`，循环条件永不满足 —— 进程 100% CPU
   空转，`socket.settimeout()` 也救不了（recv 根本不阻塞）。
2. 响应被截断时残留字节留在内核缓冲区，下一次请求读到上一次的尾巴，
   `head_bytes.decode()` 抛 `'utf-8' codec can't decode byte ...`，
   随后所有请求都在错位的字节流上失败。

这里改写 send_msg：把空返回当成死链立刻报错；解码失败则丢弃连接，
由调用方（DataEngine）负责重连重试。

另外补一个 `short_connect_timeout`：`SocketUtil.connect()` 新建 socket 时继承的是
bootstrap 设的全局 60s 超时（那是给全市场列表的大响应留的），而登录握手只有
毫秒级。IP 被服务端防火墙 DROP 时 connect 会一直等到 60s 才失败，是跑批卡死的
主要来源；登录按调用收紧超时，出门即恢复。
"""

import socket
import zlib
from contextlib import contextmanager

import baostock.common.contants as cons
import baostock.common.context as bs_context
import baostock.util.socketutil as bs_socket

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_TERMINATOR = b"<![CDATA[]]>\n"
_LOGIN_TIMEOUT = 10.0
_LOGIN_CONNECT_TIMEOUT = 8.0
# 单只股票日 K 查询的响应体很小（几十行），正常毫秒级返回；20s 足够覆盖偶发慢响应。
# 全局 60s 是留给"全市场证券列表"那种 52 万字节大响应的，套在逐只查询上会让一次
# 网络抖动拖满一分钟，5222 只里出现几次就是几分钟的无效等待。见 _send_msg。
_QUERY_TIMEOUT = 20.0
_installed = False


@contextmanager
def short_connect_timeout(seconds: float = _LOGIN_CONNECT_TIMEOUT):
    """登录期间临时收紧全局 socket 超时，让 connect() 快速失败。

    baostock 的 SocketUtil.connect() 不接受超时参数，只能靠
    socket.setdefaulttimeout 影响它新建的 socket。收窄范围严格限定在登录这一次
    调用内，退出时无论成败都恢复原值，避免波及后续依赖长超时的全市场查询。
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def current_socket():
    """返回 baostock 当前的全局连接对象。"""
    return getattr(bs_context, "default_socket", None)


def drop_connection() -> None:
    """丢弃当前连接，下次 bs.login() 会重建 socket。

    连接被污染后不要在它上面调 bs.logout()：logout 的响应同样读不完，
    可能落进上面第 1 条的空转里。
    """
    sock = current_socket()
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    try:
        delattr(bs_context, "default_socket")
    except AttributeError:
        pass


def _send_msg(msg: str):
    """库原版等价实现，区别只在失败时立刻返回 None 而不是空转。"""
    sock = current_socket()
    if sock is None:
        logger.warning("baostock 未登录，请先调用 bs.login()")
        return None

    # 按请求类型收紧超时：
    # - 登录（00）：握手毫秒级，服务端限流时会挂满整个超时，用 10s；
    # - 逐只日 K（95）：响应体很小，20s 足够，网络抖动时不至于拖满全局 60s；
    # - 其余（尤其 45 全市场列表，单次 52 万字节、耗时 60~72s）保留全局超时。
    req_type = msg.split(cons.MESSAGE_SPLIT)[1]
    is_login = req_type == cons.MESSAGE_TYPE_LOGIN_REQUEST
    is_small_query = req_type == cons.MESSAGE_TYPE_GETKDATAPLUS_REQUEST
    default_timeout = sock.gettimeout()
    try:
        if is_login:
            sock.settimeout(_LOGIN_TIMEOUT)
        elif is_small_query:
            sock.settimeout(_QUERY_TIMEOUT)
        sock.send(bytes(msg + "\n", encoding="utf-8"))
        receive = b""
        while True:
            chunk = sock.recv(8192)
            if chunk == b"":
                raise ConnectionResetError("baostock 服务端已关闭连接")
            receive += chunk
            if receive[-13:] == _TERMINATOR:
                break

        head_str = receive[0:cons.MESSAGE_HEADER_LENGTH].decode("utf-8")
        head_arr = head_str.split(cons.MESSAGE_SPLIT)
        if head_arr[1] in cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
            inner = int(head_arr[2])
            body = zlib.decompress(
                receive[cons.MESSAGE_HEADER_LENGTH:cons.MESSAGE_HEADER_LENGTH + inner]
            )
            return head_str + body.decode("utf-8")
        return receive.decode("utf-8")

    except Exception as exc:
        logger.warning(f"baostock 通信异常({type(exc).__name__}: {exc})，丢弃当前连接")
        drop_connection()
        return None
    finally:
        if is_login or is_small_query:
            try:
                sock.settimeout(default_timeout)
            except OSError:  # 连接已被 drop_connection 关掉
                pass


def install() -> None:
    """装上补丁，重复调用无副作用。"""
    global _installed
    if not _installed:
        bs_socket.send_msg = _send_msg
        _installed = True
