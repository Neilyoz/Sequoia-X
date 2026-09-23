"""鉴权路由：/api/auth/login|logout|me（T7，03-frontend-and-auth.md §3）。

本文件的纪律：
- 判定逻辑仍只有一处：login 里的 ``hmac.compare_digest`` 比的是"要不要发凭据"，
  不是"这个请求能不能放行"——后者只有 ``auth.authenticate()`` 一个实现（T3 §4.3）；
- 响应体永不含 key/token 片段与过期时刻（架构 §4 的"不泄露"约定）；
- 登录失败按 IP 限流，超限 429；未配置 API_KEY 时 login 直接 400 auth_disabled，
  否则前端会卡在"输什么 key 都失败"的困惑里（T7 §4.3）。
"""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from sequoia_x.api.auth import (
    SESSION_COOKIE_NAME,
    AuthContext,
    LoginRateLimiter,
    SessionStore,
    get_login_limiter,
    get_session_store,
    require_auth,
)
from sequoia_x.api.schemas import LoginRequest, LoginResponse, MeResponse
from sequoia_x.core.config import Settings

# 整组不加 router 级依赖：三个端点的鉴权要求各不相同，逐个显式声明比"挂满再豁免"清楚。
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    """限流计数的键。TestClient/反代后都可能取不到真实对端，统一退化成 "unknown" 共享配额
    ——退化方向是"更容易触发 429"而不是"限流失效"，这是可接受的偏保守。"""
    return request.client.host if request.client is not None else "unknown"


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    store: SessionStore = Depends(get_session_store),
    limiter: LoginRateLimiter = Depends(get_login_limiter),
) -> dict[str, object]:
    """用 API_KEY 换一个 HttpOnly session cookie（03 §2：零新增凭据）。

    该接口自身**不要求** ``X-Sequoia-Client`` 头：那时还没有 session 可言，加了
    就等于"要登录才能登录"。它自身也不构成 CSRF 面（登录跨站最多让用户登进自己的会话）。
    """
    settings: Settings = request.app.state.settings
    if not settings.api_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "auth_disabled",
                "hint": "服务处于无鉴权模式，无需登录",
            },
        )
    client_ip = _client_ip(request)
    if limiter.is_blocked(client_ip):
        retry_after = limiter.retry_after_seconds(client_ip)
        raise HTTPException(
            status_code=429,
            detail={"code": "login_rate_limited", "retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
    # 常量时间比较：失败原因不区分"key 错"与"key 空"，也不回显任何片段。
    if not hmac.compare_digest(body.api_key, settings.api_key):
        limiter.record_failure(client_ip)
        raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})

    limiter.record_success(client_ip)
    # 03 §3：不开后台清理线程，登录这个低频入口就是顺手回收的时机。
    store.purge_expired()
    token = store.issue(settings.session_ttl_seconds)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_seconds,
        path="/",
        # httponly：JS 读不到 token，XSS 拿不走会话；secure 刻意不设——
        # 本项目默认监听 127.0.0.1 的 http，设了 cookie 根本存不下来（T8 上 https 时再开）。
        httponly=True,
        samesite="strict",  # CSRF 主力防线：跨站请求不带 cookie（03 §4 第 1 道）
    )
    return {"authenticated": True, "expires_in": settings.session_ttl_seconds}


@router.post("/logout", status_code=204, response_class=Response)
def logout(
    request: Request,
    response: Response,
    store: SessionStore = Depends(get_session_store),
) -> None:
    """撤销当前会话并下发过期 cookie。无 cookie 也返回 204：登出必须幂等，
    给前端回 401 只会让"退出"按钮变成报错。

    与 login 一样不挂 ``require_auth``——它是凭据交换端点，不参与鉴权判定，
    因此天然不受 CSRF 写守卫约束。03 §4 要求的豁免清单就是这两条路由本身，
    豁免靠"不依赖带守卫的依赖"表达，绝不写 ``if path == "/api/auth/login"``。
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        store.revoke(token)
    # path 必须与 set_cookie 一致（"/"），否则浏览器认为是要删另一个 cookie，旧的留下。
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    # FastAPI 默认用 JSONResponse 造响应体，会给 204 也挂上 Content-Type；
    # RFC 9110 说 204 不该声明媒体类型，所以 response_class=Response + 显式删头。
    if "content-type" in response.headers:
        del response.headers["content-type"]
    return None


@router.get("/me", response_model=MeResponse)
def me(auth: AuthContext = Depends(require_auth)) -> dict[str, object]:
    """前端应用初始化时问"我还要不要登录"。

    mode 直接来自 require_auth 的判定结果（唯一实现处），open 模式返回 mode=open，
    前端据此跳过登录页——本地开发不配 API_KEY 也能直接看到看板。
    """
    return {"authenticated": True, "mode": auth.mode}
