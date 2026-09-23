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
"""

import zlib

import baostock.common.contants as cons
import baostock.common.context as bs_context
import baostock.util.socketutil as bs_socket

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_TERMINATOR = b"<![CDATA[]]>\n"
_installed = False


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

    try:
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


def install() -> None:
    """装上补丁，重复调用无副作用。"""
    global _installed
    if not _installed:
        bs_socket.send_msg = _send_msg
        _installed = True
