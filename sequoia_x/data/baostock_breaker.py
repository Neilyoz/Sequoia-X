"""baostock 熔断状态持久化。

服务端的封禁发生在**网络层**：API 服务器（public-api.baostock.com:10030）的
防火墙直接丢弃本机公网 IP 的数据包，表现是 TCP 连接超时，而不是 `10001011`
那类应用层错误码。实测这种状态下目标主机所有端口都不回包（80/443 同样超时），
而官网主机、以及全球其他节点访问都正常 —— 即"源 IP 被 DROP"。

封禁会持续数小时到数天，这段时间里每一次登录尝试都只是白等一个超时。把
"不可达"这个事实写到磁盘，下一轮跑批就能直接短路到本地数据，而不是再去撞墙。

冷却窗口指数增长（10 分钟起、6 小时封顶）：跑批是低频调用，几乎感知不到；
常驻服务被人反复触发时则能挡住连接风暴。换出口 IP（拨号重连、切网络、走代理）
后要立刻恢复，执行 `python main.py --reset-baostock` 清掉状态即可。
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_ENV_PATH = "BAOSTOCK_BREAKER_PATH"
_DEFAULT_PATH = "data/baostock_breaker.json"
_BACKOFF_BASE_SECONDS = 600  # 首次失败冷却 10 分钟
_BACKOFF_MAX_SECONDS = 6 * 3600  # 封顶 6 小时


def state_path() -> Path:
    """状态文件位置。环境变量优先，便于测试与多环境隔离。"""
    return Path(os.environ.get(_ENV_PATH, _DEFAULT_PATH))


def load() -> dict:
    """读状态。文件缺失或损坏一律当作"没有失败历史"，绝不阻断主流程。"""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def backoff_seconds(failures: int) -> int:
    """失败次数 -> 冷却时长：10min、20min、40min……6h 封顶。"""
    if failures <= 0:
        return 0
    return min(_BACKOFF_BASE_SECONDS * 2 ** (failures - 1), _BACKOFF_MAX_SECONDS)


def cooldown_remaining() -> float:
    """剩余冷却秒数；<= 0 表示可以发起连接。"""
    until = _parse_time(load().get("blocked_until"))
    if until is None:
        return 0.0
    return max(0.0, (until - _now()).total_seconds())


def record_failure(reason: str) -> dict:
    """记一次不可达并写回冷却窗口，返回新状态供调用方打日志。"""
    failures = _as_int(load().get("failures")) + 1
    wait = backoff_seconds(failures)
    now = _now()
    state = {
        "failures": failures,
        "reason": reason,
        "failed_at": now.isoformat(timespec="seconds"),
        "blocked_until": (now + timedelta(seconds=wait)).isoformat(timespec="seconds"),
    }
    _write(state)
    logger.warning(
        f"baostock 不可达（{reason}），连续第 {failures} 次，"
        f"{wait // 60} 分钟内不再发起连接，避免刷新服务端封禁"
    )
    return state


def record_success() -> None:
    """登录成功即清除熔断 —— 只有真拿到成功响应才算恢复。"""
    reset()


def reset() -> None:
    """清除熔断状态。换出口 IP 后由 `--reset-baostock` 调到这里。"""
    try:
        state_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"baostock 熔断状态清除失败：{exc}")


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _write(state: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"baostock 熔断状态写入失败：{exc}")
