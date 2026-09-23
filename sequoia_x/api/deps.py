"""依赖注入：鉴权依赖接线 + 从 app.state 取进程级对象（架构 §3.8）。

刻意不使用模块级全局单例：所有对象都从 ``request.app.state`` 取，这样测试才能
构造互不污染的第二个 app（架构 §3.8）。本模块 import 了 task 层符号，
因此只能在 ``sequoia_x.api.app``（其模块顶层已先执行 bootstrap）之后被导入。

T7 起鉴权判定本体迁到 ``api/auth.py``（会话表是判定的必需前置），本模块只负责
"路由 → 鉴权依赖"的接线，路由侧统一只见 ``require_auth``。
"""

from typing import TYPE_CHECKING

from fastapi import Request

from sequoia_x.api.auth import (
    AuthContext,
    LoginRateLimiter,
    SessionStore,
    api_key_header,
    authenticate,
    get_login_limiter,
    get_session_store,
    require_auth,
)
from sequoia_x.core.config import Settings
from sequoia_x.task.manager import TaskManager
from sequoia_x.task.store import TaskStore

if TYPE_CHECKING:  # 仅为类型标注引入，运行期不让 deps 提前拉起 baostock 守卫
    from sequoia_x.data.engine import DataEngine

# 显式声明"从 auth 层转出来的名字"是本模块的公开面，避免 ruff 把 re-export 判成未用导入。
__all__ = [
    "AuthContext",
    "LoginRateLimiter",
    "SessionStore",
    "api_key_header",
    "authenticate",
    "get_engine",
    "get_login_limiter",
    "get_manager",
    "get_session_store",
    "get_settings_dep",
    "get_store",
    "require_api_key",
    "require_auth",
]

# T3 兼容别名：语义与 require_auth **完全一致**（含 cookie 分支与 CSRF 写守卫）。
# 刻意用别名而不是"再写一个只认 header 的依赖"——两条判定路径迟早分叉，
# 漏改一处就是静默的鉴权降级。新代码一律用 require_auth。
require_api_key = require_auth


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
