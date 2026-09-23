"""会话表、鉴权判定与 CSRF 写守卫（03-frontend-and-auth.md §3/§4，T7）。

``authenticate()`` 是判定逻辑的**唯一实现处**（四步顺序见其 docstring）：路由、依赖、
登录接口都从这里出，别处再写一条分支就等于开了一个"改一处漏一处"的鉴权洞（T3 §4.3）。

刻意不使用模块级会话单例：SessionStore / LoginRateLimiter 的宿主是 ``app.state``
（lifespan 里建），否则第二个测试 app 会串味（T3 已确立此约定）。代价是进程重启
所有人重新登录一次 —— 单进程部署本就是本项目前提（约束 §3、03 §9）。
"""

import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from sequoia_x.core.config import Settings

# cookie 名是前后端契约（T8 的登录页与浏览器都要靠它），集中在此定义供测试导入。
SESSION_COOKIE_NAME = "sequoia_session"
# CSRF 第二道防线：跨站表单发不出自定义头，浏览器端的写请求必须带 X-Sequoia-Client: web。
CLIENT_HEADER_NAME = "X-Sequoia-Client"
CLIENT_HEADER_VALUE = "web"

# 登录失败限流：API_KEY 是长随机串，理论爆破成本极高，但服务可能被暴露，
# 所以要有一道闸（T7 §4.3）。数值写死在代码里而不进 Settings：这是防护参数，
# 调它的正确姿势是读这段注释，而不是去 .env 里试着关掉。
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW_SECONDS = 60

# 03 §4：只有这四个方法是"写"。GET/HEAD/OPTIONS 不要求 X-Sequoia-Client。
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# auto_error=False：key 缺失与 key 错误都收敛到 authenticate() 的同一个 401，
# 保证两种失败形态响应完全一致（架构 §4：不区分"key 不存在"与"key 不对"）。
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    """一次鉴权判定的结果。

    ``mode`` 取值是对外契约（``GET /api/auth/me`` 会原样暴露，前端据此决定是否跳登录页）：
    - ``open``：未配置 API_KEY，服务运行在无鉴权模式；
    - ``session``：凭合法的 session cookie 放行；
    - ``apikey``：凭 X-API-Key 头放行。
    """

    mode: str


class SessionStore:
    """进程内会话表 ``dict[token, 过期时刻]``，不落库（03 §3）。

    用 ``time.monotonic()`` 记过期而非墙钟时间：本机改系统时间不该让已发出去的
    会话凭空延长或提前失效。
    """

    def __init__(self) -> None:
        self._expires_at: dict[str, float] = {}

    def issue(self, ttl_seconds: int) -> str:
        """签发一个新会话并返回 token（token 即 cookie 值）。"""
        token = secrets.token_urlsafe(32)
        self._expires_at[token] = time.monotonic() + ttl_seconds
        return token

    def is_valid(self, token: str) -> bool:
        """token 存在且未过期才算有效；顺手回收已过期条目。

        逐个 ``compare_digest`` 而不是 ``token in dict``：字典查表会因命中位置不同
        产生可测的时序差异，等于给爆破者一个"前缀对不对"的旁路。会话表常态下只有
        个位数条目，O(n) 的代价可以忽略。
        """
        stored = self._find(token)
        if stored is None:
            return False
        if self._expires_at[stored] <= time.monotonic():
            del self._expires_at[stored]
            return False
        return True

    def revoke(self, token: str) -> None:
        """撤销一个会话（登出用）。不存在则静默——登出必须幂等。"""
        stored = self._find(token)
        if stored is not None:
            del self._expires_at[stored]

    def purge_expired(self) -> int:
        """清掉所有已过期条目，返回回收条数。

        调用时机是每次登录（03 §3：不开后台清理线程）。启动时不调——会话表随进程
        新建，必然为空（见 app.py 的 lifespan 注释）。
        """
        now = time.monotonic()
        expired = [token for token, expires in self._expires_at.items() if expires <= now]
        for token in expired:
            del self._expires_at[token]
        return len(expired)

    def __len__(self) -> int:
        """当前**未过期**会话数，供 /api/info 观测（过期条目可能还没被回收）。"""
        now = time.monotonic()
        return sum(1 for expires in self._expires_at.values() if expires > now)

    def _find(self, token: str) -> str | None:
        """常量时间找到 token 在表里的实际键；空 cookie 值直接 False，不进比较。"""
        if not token:
            return None
        for stored in self._expires_at:
            if hmac.compare_digest(stored, token):
                return stored
        return None


class LoginRateLimiter:
    """按客户端 IP 记登录失败次数，1 分钟内 5 次即拒。

    进程内 dict、重启清零（与 SessionStore 同理：单进程部署前提，且不为它引入
    slowapi 之类新依赖，T7 §4.3）。"重启即重置计数器"不构成缺陷——能重启服务的人
    本来就读得到 .env 里的 API_KEY，这道闸防的是远程爆破，不是本地提权。
    """

    def __init__(
        self,
        limit: int = LOGIN_FAIL_LIMIT,
        window_seconds: int = LOGIN_FAIL_WINDOW_SECONDS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._failures: dict[str, list[float]] = {}

    def is_blocked(self, client_ip: str) -> bool:
        """窗口内失败次数已达上限即为 True（只查不计数）。"""
        return len(self._recent(client_ip)) >= self._limit

    def record_failure(self, client_ip: str) -> None:
        """记一次失败。写入前先清过期键，避免长时间运行后 dict 无界增长。"""
        self._prune()
        self._failures.setdefault(client_ip, []).append(time.monotonic())

    def record_success(self, client_ip: str) -> None:
        """登录成功即清零：不让用户的历史手滑拖累下一次登录。"""
        self._failures.pop(client_ip, None)

    def retry_after_seconds(self, client_ip: str) -> int:
        """还有多少秒解除限制（供 429 的 Retry-After）。"""
        recent = self._recent(client_ip)
        if not recent:
            return 0
        return max(1, int(self._window - (time.monotonic() - min(recent))) + 1)

    def _recent(self, client_ip: str) -> list[float]:
        """取窗口内的失败时间戳。没见过的 IP 不写入空列表——否则扫描器换着 IP 试
        就能把这个 dict 撑大（服务只监听回环，但登录接口是所有可达路径里最热的）。"""
        now = time.monotonic()
        stamps = [t for t in self._failures.get(client_ip, ()) if now - t < self._window]
        if stamps or client_ip in self._failures:
            self._failures[client_ip] = stamps
        return stamps

    def _prune(self) -> None:
        for client_ip in list(self._failures):
            if not self._recent(client_ip):
                del self._failures[client_ip]


def authenticate(
    request: Request,
    settings: Settings,
    store: SessionStore,
    x_api_key: str | None = None,
) -> AuthContext:
    """鉴权判定的**唯一**入口，四步顺序即 03 §3.4，不得增删或换序。

    1. 未配置 API_KEY → 直接放行（本地开发语义，启动时已打 WARNING，不逐请求刷屏）；
    2. 请求带合法 session cookie → 放行；
    3. 请求带正确 X-API-Key → 放行；
    4. 否则统一 401，detail 不回显 key/token 的任何片段、不回显过期时间。

    cookie 先于 header 是刻意的（03 §3.4 的原序）：浏览器每次请求都自动带 cookie，
    这条路径命中即结束；X-API-Key 只有脚本/curl 才会带，放在后面不影响主路径。
    换序不会带来安全问题，但会让每次页面请求都多一次字符串比较与一条误导性的
    "先查密钥"读法。

    token 与 key 的比较都用 ``hmac.compare_digest``（常量时间），理由同 SessionStore：
    成本为零，没有理由用 ``==``。
    """
    if not settings.api_key:
        return AuthContext(mode="open")
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token and store.is_valid(token):
        return AuthContext(mode="session")
    if x_api_key is not None and hmac.compare_digest(x_api_key, settings.api_key):
        return AuthContext(mode="apikey")
    raise HTTPException(status_code=401, detail={"code": "unauthorized"})


def require_write_guard(request: Request, auth: AuthContext) -> None:
    """cookie 模式下的写操作必须带 ``X-Sequoia-Client: web``（03 §4 第 2 道防线）。

    只针对 ``mode == "session"``：
    - ``apikey`` 是显式鉴权，攻击者拿不到头就没法构造请求，不存在 CSRF 面（03 §4）；
    - ``open`` 模式压根不发 cookie，加了守卫只会让 curl 脚本莫名多出 403。
    """
    if auth.mode != "session":
        return
    if request.method not in _WRITE_METHODS:
        return
    if request.headers.get(CLIENT_HEADER_NAME, "").lower() != CLIENT_HEADER_VALUE:
        raise HTTPException(status_code=403, detail={"code": "missing_client_header"})


class RequireAuth:
    """把 authenticate() 接成 FastAPI 依赖；``write_guard=False`` 是显式豁免开关。

    豁免写成依赖参数而不是 ``if request.url.path == "/api/auth/login"``：路径字符串判断
    会在改前缀/加路由时长出漏网之鱼，而这里的豁免清单必须一眼数得清
    （凭据交换类接口 login/logout）。
    """

    def __init__(self, write_guard: bool = True) -> None:
        self.write_guard = write_guard

    def __call__(
        self,
        request: Request,
        x_api_key: str | None = Security(api_key_header),
    ) -> AuthContext:
        """取头 → 判定 → （可选）写守卫。判定本身一行都不在这里。"""
        auth = authenticate(
            request,
            request.app.state.settings,
            request.app.state.sessions,
            x_api_key,
        )
        if self.write_guard:
            require_write_guard(request, auth)
        return auth


# 默认依赖：受保护业务路由全部挂它（router 级声明，漏一个就是安全洞）。
# write_guard 开关目前只有 True 这条线在产线上跑；False 一支由 T7 测试直接构造
# （见 tests/test_api_auth_session.py），用来钉住"守卫与判定是两件事"这条边界，
# 也留给未来确实需要豁免的写接口——届时新增一个显式实例，不要退回路径字符串判断。
require_auth = RequireAuth()


def get_session_store(request: Request) -> SessionStore:
    """取 lifespan 建好的会话表（统一入口，避免路由直接摸 app.state）。"""
    store: SessionStore = request.app.state.sessions
    return store


def get_login_limiter(request: Request) -> LoginRateLimiter:
    """取 lifespan 建好的登录限流器。"""
    limiter: LoginRateLimiter = request.app.state.login_limiter
    return limiter
