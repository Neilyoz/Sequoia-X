"""依赖注入：鉴权判定唯一入口 + 从 app.state 取进程级对象（架构 §3.8）。

刻意不使用模块级全局单例：所有对象都从 ``request.app.state`` 取，这样测试才能
构造互不污染的第二个 app（架构 §3.8）。本模块 import 了 task 层符号，
因此只能在 ``sequoia_x.api.app``（其模块顶层已先执行 bootstrap）之后被导入。
"""

import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from sequoia_x.core.config import Settings
from sequoia_x.task.manager import TaskManager
from sequoia_x.task.store import TaskStore

if TYPE_CHECKING:  # 仅为类型标注引入，运行期不让 deps 提前拉起 baostock 守卫
    from sequoia_x.data.engine import DataEngine

# auto_error=False：key 缺失与 key 错误都收敛到 authenticate() 的同一个 401，
# 保证两种失败形态响应完全一致（架构 §4：不区分"key 不存在"与"key 不对"）。
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    """一次鉴权判定的结果。

    ``mode`` 取值是对外契约（T7 的 ``GET /api/auth/me`` 会原样暴露）：
    - ``open``：未配置 API_KEY，服务运行在无鉴权模式；
    - ``apikey``：凭 X-API-Key 头放行；
    - ``session``：T7 将新增（HttpOnly session cookie）。
    """

    mode: str


def authenticate(request: Request, x_api_key: str | None = None) -> AuthContext:
    """鉴权判定的**唯一**入口；判定逻辑禁止散落到路由体或各依赖函数里（T3 §4.3）。

    判定顺序与 03-frontend-and-auth.md §3 对齐（session 分支是 T7 的活）：
    1. 未配置 API_KEY → 直接放行（本地开发语义，启动时已打 WARNING）；
    2. T7：请求带合法 session cookie → 放行；
    3. 请求带正确 X-API-Key → 放行；
    4. 否则统一 401，detail 不回显 key 的任何片段。

    用 ``hmac.compare_digest`` 做常量时间比较：本地服务被时序攻击的概率极低，
    但成本为零，没有理由用 ``==``。

    T7 接线说明：届时在本函数内加入 ``request.app.state.sessions`` 的 cookie
    分支，并把本函数与 :class:`AuthContext` 迁移到 ``api/auth.py``；
    ``require_api_key`` 与各 router 的依赖声明都不用动。
    """
    settings: Settings = request.app.state.settings
    if not settings.api_key:
        return AuthContext(mode="open")
    if x_api_key is not None and hmac.compare_digest(x_api_key, settings.api_key):
        return AuthContext(mode="apikey")
    raise HTTPException(status_code=401, detail={"code": "unauthorized"})


def require_api_key(
    request: Request,
    x_api_key: str | None = Security(api_key_header),
) -> AuthContext:
    """薄包装依赖：只做"取头 → 交给 authenticate()"，不含任何判定逻辑。

    挂在 APIRouter 的 dependencies 上（router 级声明），禁止逐函数各挂一遍——
    漏一个就是安全洞（T3 §4.3）。
    """
    return authenticate(request, x_api_key)


def get_settings_dep(request: Request) -> Settings:
    """取 app 级 Settings（统一入口，避免路由直接摸 app.state）。"""
    settings: Settings = request.app.state.settings
    return settings


def get_manager(request: Request) -> TaskManager:
    """取 lifespan 建好的 TaskManager。"""
    manager: TaskManager = request.app.state.manager
    return manager


def get_store(request: Request) -> TaskStore:
    """取 lifespan 建好的 TaskStore。"""
    store: TaskStore = request.app.state.store
    return store


def get_engine(request: Request) -> "DataEngine":
    """取只读查询可用的 DataEngine（T3 路由未用到，为 T5 预埋，架构 §3.8）。"""
    engine: DataEngine = request.app.state.engine
    return engine
