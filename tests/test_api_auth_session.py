"""T7 会话鉴权测试（T7 §5，03-frontend-and-auth.md §3/§4）。

三条主线：
1. 判定四步顺序（open → session cookie → X-API-Key → 401）逐条落到断言上；
2. CSRF 写守卫只对 cookie 模式的写请求生效，X-API-Key 直连与 open 模式不受影响；
3. 凭据不泄露：401/403 响应体不含 API_KEY 片段、不含 token、不含过期时刻。

一律 Settings(_env_file=None) 注入假配置 + db_path 指向 tmp（红线：绝不碰真实行情库）；
所有会提交任务的用例都 patch 掉 run_daily，绝不真跑 baostock/飞书（约束 §6/§9）。
"""

import logging
import os
import time
from unittest.mock import patch

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient

from sequoia_x.api.app import create_app
from sequoia_x.api.auth import (
    SESSION_COOKIE_NAME,
    RequireAuth,
    SessionStore,
    authenticate,
)
from sequoia_x.api.deps import require_api_key, require_auth
from sequoia_x.core.config import Settings
from sequoia_x.runner.pipeline import DailyReport, StrategyOutcome

API_KEY = "sekret"
AUTH = {"X-API-Key": API_KEY}
WEB = {"X-Sequoia-Client": "web"}
TTL = 1234  # 故意取一个不像默认值 604800 的数，确保 Max-Age 真来自配置
RUN_DAILY = "sequoia_x.runner.pipeline.run_daily"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _detach_task_log_captures() -> None:
    """摘除 lifespan 挂上的 TaskLogCapture：T2 的挂载是进程级的且只挂不摘，
    不清扫会让 test_logger.py 属性 3（新建 logger 只应有 1 个 handler）假红。"""
    from sequoia_x.core import logger as core_logger
    from sequoia_x.task.logs import TaskLogCapture

    core_logger._extra_handlers[:] = [
        h for h in core_logger._extra_handlers if not isinstance(h, TaskLogCapture)
    ]
    for lg in logging.Logger.manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            lg.handlers[:] = [h for h in lg.handlers if not isinstance(h, TaskLogCapture)]


@pytest.fixture(autouse=True)
def clean_task_log_captures():
    yield
    _detach_task_log_captures()


def _build(tmp_path, **overrides):
    """构造 (app, client)：鉴权开、TTL 用非默认值、静态托管关掉（由 test_static_hosting 专测）。"""
    kwargs: dict[str, object] = {
        "db_path": str(tmp_path / "market_stub.db"),
        "task_db_path": str(tmp_path / "tasks.db"),
        "api_key": API_KEY,
        "session_ttl_seconds": TTL,
        "serve_frontend": False,
    }
    kwargs.update(overrides)  # 允许把 api_key 覆盖成空串以测 open 模式
    settings = Settings(_env_file=None, feishu_webhook_url="http://feishu.invalid/hook", **kwargs)
    app = create_app(settings)
    return app, TestClient(app)


@pytest.fixture
def app_and_client(tmp_path):
    """限流器与会话表都挂在 app.state 上，所以每个用例一套，测试之间不串味。"""
    return _build(tmp_path)


def _login(client: TestClient, api_key: str = API_KEY):
    return client.post("/api/auth/login", json={"api_key": api_key})


def _login_ok(client: TestClient) -> str:
    resp = _login(client)
    assert resp.status_code == 200, resp.text
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token
    return token


def fake_report() -> DailyReport:
    """与真实 pipeline 同形态的假结果，供被 patch 的 run_daily 返回（不落库、不外呼）。"""
    return DailyReport(
        trade_date="2026-09-23",
        synced_rows=1,
        outcomes=[
            StrategyOutcome(
                strategy_name="MaVolumeStrategy",
                webhook_key="ma_volume",
                symbols=["600000"],
                pushed=False,
            )
        ],
        push_enabled=False,
    )


# ── 登录 / 登出主流程 ──


def test_login_success_sets_hardened_cookie(app_and_client) -> None:
    """守住的性质：cookie 的四个属性一个都不能少（03 §4 第 1 道防线全靠 SameSite）。

    Max-Age 必须等于 SESSION_TTL_SECONDS：写死在代码里的话，改了配置前端仍会拿到
    过期时间不对的会话，而这类偏差在浏览器里几乎看不出来。
    """
    _, client = app_and_client
    with client:
        resp = _login(client)
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": True, "expires_in": TTL}
        cookie = resp.headers["set-cookie"].lower()
        assert f"{SESSION_COOKIE_NAME}=" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert f"max-age={TTL}" in cookie
        assert "path=/" in cookie
        # token 只出现在 Set-Cookie 里，body 一个字都不带（前端不需要也不该持有它）
        assert client.cookies.get(SESSION_COOKIE_NAME) not in resp.text


def test_login_wrong_key_returns_401_and_leaks_nothing(app_and_client) -> None:
    """守住的性质：登录失败不回显 key 的任何片段（T7 §5 点名的防泄露断言）。"""
    _, client = app_and_client
    with client:
        resp = _login(client, "wr0ng-and-longer-key")
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"code": "invalid_credentials"}
        assert API_KEY not in resp.text
        assert "ekret" not in resp.text


def test_login_empty_key_is_rejected_not_treated_as_open(app_and_client) -> None:
    """守住的性质：空串走同一条 401 分支，不能被误判成"服务未配 key 所以放行"。"""
    _, client = app_and_client
    with client:
        assert _login(client, "").status_code == 401


def test_login_body_missing_field_is_422(app_and_client) -> None:
    """守住的性质：body 是 pydantic 模型，缺字段由 FastAPI 校验拦下，不进鉴权逻辑。"""
    _, client = app_and_client
    with client:
        assert client.post("/api/auth/login", json={}).status_code == 422


def test_unauthorized_and_invalid_credentials_codes_differ(app_and_client) -> None:
    """守住的性质：两个错误码必须不同——前端据此决定"跳登录页"还是"提示密码错"（T7 §4.2）。"""
    _, client = app_and_client
    with client:
        unauthorized = client.get("/api/tasks").json()["detail"]["code"]
        bad_credentials = _login(client, "x").json()["detail"]["code"]
        assert unauthorized == "unauthorized"
        assert bad_credentials == "invalid_credentials"
        assert unauthorized != bad_credentials


def test_session_cookie_grants_access_and_removal_revokes(app_and_client) -> None:
    """守住的性质：cookie 与 X-API-Key 二者之一即可放行（T7 §1 的升级目标）。"""
    _, client = app_and_client
    with client:
        _login_ok(client)
        assert client.get("/api/tasks").status_code == 200
        assert client.get("/api/info").status_code == 200
        # 删掉 cookie 后浏览器不再带凭据 → 回到 401（会话不是"看过就算"的缓存）
        client.cookies.clear()
        resp = client.get("/api/tasks")
        assert resp.status_code == 401
        assert resp.json()["detail"] == {"code": "unauthorized"}


def test_api_key_direct_access_needs_no_client_header(app_and_client) -> None:
    """守住的性质：X-API-Key 直连不要求 X-Sequoia-Client（03 §4：显式鉴权无 CSRF 面）。

    这条是脚本/curl 的兼容性契约——T3 的全部用法必须原样能跑。
    """
    _, client = app_and_client
    with client:
        assert client.get("/api/tasks", headers=AUTH).status_code == 200
        assert client.get("/api/strategies", headers=AUTH).status_code == 200


def test_me_reports_mode_for_every_auth_path(app_and_client) -> None:
    """守住的性质：/api/auth/me 的 mode 就是 authenticate() 四步的落点（前端只信这个字段）。"""
    _, client = app_and_client
    with client:
        assert client.get("/api/auth/me", headers=AUTH).json() == {
            "authenticated": True,
            "mode": "apikey",
        }
        _login_ok(client)
        assert client.get("/api/auth/me").json() == {"authenticated": True, "mode": "session"}
        client.cookies.clear()
        assert client.get("/api/auth/me").status_code == 401


def test_logout_kills_cookie_immediately(app_and_client) -> None:
    """守住的性质：logout 后原 token 立刻失效（服务端 revoke，而不是只清 cookie）。

    只清客户端 cookie 的"假登出"在 token 被别人抄走时毫无用处。
    """
    app, client = app_and_client
    with client:
        token = _login_ok(client)
        assert client.get("/api/info").status_code == 200
        assert client.post("/api/auth/logout", headers=WEB).status_code == 204
        assert len(app.state.sessions) == 0
        # 手工把旧 token 塞回去：服务端已经不认了
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE_NAME, token)
        assert client.get("/api/info").status_code == 401


def test_logout_without_cookie_is_idempotent_204(app_and_client) -> None:
    """守住的性质：无 cookie 也返回 204（T7 §4.3：登出不得给前端造 401）。

    顺带钉住 204 的形态：空 body、无 Content-Type（FastAPI 默认会塞一个，与"无内容"
    自相矛盾），并始终下发清除用的 Set-Cookie。
    """
    _, client = app_and_client
    with client:
        for resp in (
            client.post("/api/auth/logout"),
            client.post("/api/auth/logout", headers=AUTH),
        ):
            assert resp.status_code == 204
            assert resp.content == b""
            assert "content-type" not in resp.headers
            assert f'{SESSION_COOKIE_NAME}=""' in resp.headers["set-cookie"]


def test_expired_session_rejected_then_reclaimed(tmp_path) -> None:
    """守住的性质：过期判定看服务端时钟，而不是 cookie 里的 Max-Age。

    浏览器可能因为时钟差异继续发送服务端已过期的 cookie，这时必须 401；
    回收有两条路径（is_valid 惰性删、purge_expired 批量删），都得测到，
    否则登录一路攒垃圾也没人发现。
    """
    app, client = _build(tmp_path)
    with client:
        store: SessionStore = app.state.sessions  # 会话表由 lifespan 装配，进上下文后才存在
        token = _login_ok(client)
        store._expires_at[token] = time.monotonic() - 1  # 手工改到过去
        assert client.get("/api/info").status_code == 401
        assert len(store) == 0
        # 没人访问过的过期条目由 purge_expired 回收（登录时机的路径）
        orphan = store.issue(TTL)
        store._expires_at[orphan] = time.monotonic() - 1
        assert len(store) == 0
        assert store.purge_expired() == 1


def test_purge_expired_does_not_touch_live_sessions(tmp_path) -> None:
    """守住的性质：purge 只清死的（回收数与剩余数必须自洽，误伤即全员被踢）。"""
    store = SessionStore()
    live = store.issue(TTL)
    dead = store.issue(TTL)
    store._expires_at[dead] = time.monotonic() - 1
    assert len(store) == 1
    assert store.purge_expired() == 1
    assert store.is_valid(live) is True
    assert len(store) == 1


# ── CSRF 写守卫 ──


def test_cookie_write_without_client_header_is_403(app_and_client) -> None:
    """守住的性质：cookie 模式的写请求缺 X-Sequoia-Client 一律 403（03 §4 第 2 道防线）。

    403 而不是 401：身份是好的，缺的是"这个请求来自本站页面"的证明。
    守卫在 body 校验与提交之前，所以副作用为零。
    """
    _, client = app_and_client
    with client:
        _login_ok(client)
        resp = client.post("/api/tasks/daily", json={})
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"code": "missing_client_header"}
        assert client.get("/api/tasks", headers=AUTH).json()["total"] == 0


def test_cookie_write_with_bogus_client_header_is_still_403(app_and_client) -> None:
    """守住的性质：头的值是"意图证明"而不是"存在性证明"——随手加个自定义头不算通过。

    否则攻击者页面里塞任意 X-Sequoia-Client: whatever 就绕开了第二道防线。
    """
    _, client = app_and_client
    with client:
        _login_ok(client)
        resp = client.post("/api/tasks/daily", json={}, headers={"X-Sequoia-Client": "curl"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"code": "missing_client_header"}


def test_cookie_read_does_not_need_client_header(app_and_client) -> None:
    """守住的性质：GET 不要求自定义头（否则每次刷新都要先跑一遍预检）。"""
    _, client = app_and_client
    with client:
        _login_ok(client)
        assert client.get("/api/signals", params={"date": "2026-01-01"}).status_code == 200


def test_cookie_write_with_client_header_is_accepted(app_and_client) -> None:
    """守住的性质：带上 X-Sequoia-Client: web 的 cookie 写请求正常 202（前端唯一路径）。

    run_daily 被 patch：这条测的是"守卫放行"，不是跑批本身，绝不打 baostock/飞书。
    """
    app, client = app_and_client
    with client, patch(RUN_DAILY, return_value=fake_report()):
        _login_ok(client)
        resp = client.post("/api/tasks/daily", json={}, headers=WEB)
        assert resp.status_code == 202
        assert resp.json()["kind"] == "daily"
        # 等后台任务收尾：lifespan 关闭时线程池里不留活（约束 §3 的单飞语义）
        app.state.manager.wait_for_completion(resp.json()["task_id"], timeout=15)


def test_api_key_write_needs_no_client_header(app_and_client) -> None:
    """守住的性质：X-API-Key 模式的写请求不受守卫约束（T3 的 curl 用法零改动）。"""
    app, client = app_and_client
    with client, patch(RUN_DAILY, return_value=fake_report()):
        resp = client.post("/api/tasks/daily", json={}, headers=AUTH)
        assert resp.status_code == 202
        app.state.manager.wait_for_completion(resp.json()["task_id"], timeout=15)


def test_write_guard_can_be_disabled_explicitly(tmp_path) -> None:
    """守住的性质：write_guard=False 是显式豁免开关（T7 §4.2：豁免不靠路径字符串判断）。

    产线上 login/logout 靠"压根不挂 require_auth"来豁免；这个开关留给确实需要
    "已鉴权但不要求自定义头"的写接口。这里直接构造一个，防止没人用的分支悄悄腐掉。
    """
    app, _ = _build(tmp_path)
    unguarded = RequireAuth(write_guard=False)

    @app.post("/probe/unguarded", dependencies=[Depends(unguarded)])
    def probe() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        _login_ok(client)
        assert client.post("/probe/unguarded").status_code == 200


# ── 登录限流 ──


def test_login_rate_limited_per_ip(app_and_client) -> None:
    """守住的性质：1 分钟内 5 次失败后第 6 次直接 429，且不校验凭据（闸要先落下）。"""
    app, client = app_and_client
    with client:
        for i in range(5):
            assert _login(client, f"bad-{i}").status_code == 401, i
        blocked = _login(client, "bad-5")
        assert blocked.status_code == 429
        assert blocked.json()["detail"]["code"] == "login_rate_limited"
        assert blocked.headers["retry-after"].isdigit()
        # 限流期间连正确的 key 也进不去：429 的语义就是"这段时间别再试"
        assert _login(client, API_KEY).status_code == 429
        assert len(app.state.sessions) == 0


def test_login_success_clears_failure_counter(app_and_client) -> None:
    """守住的性质：登录成功清零计数（用户手滑几次后仍能正常登录，不被历史拖累）。"""
    _, client = app_and_client
    with client:
        for _ in range(4):
            assert _login(client, "bad").status_code == 401
        assert _login(client, API_KEY).status_code == 200
        # 计数器已清零 → 这一次失败仍是 401 而不是 429
        assert _login(client, "bad").status_code == 401


def test_rate_limiter_window_rolls_back(app_and_client) -> None:
    """守住的性质：限流按时间窗滚动，窗口过后自动放行（不需要后台线程来恢复）。"""
    app, client = app_and_client
    with client:
        for _ in range(5):
            assert _login(client, "bad").status_code == 401
        assert _login(client, "bad").status_code == 429
        limiter = app.state.login_limiter
        failures = limiter._failures["testclient"]
        # 把时间戳整体推到窗口之外（等价于等满 60 秒，但不让测试真睡一分钟）
        limiter._failures["testclient"] = [t - limiter._window - 1 for t in failures]
        assert limiter.is_blocked("testclient") is False
        assert _login(client, API_KEY).status_code == 200


# ── open 模式（用户真实 .env 的主路径）──


def test_open_mode_serves_everything_without_credentials(tmp_path) -> None:
    """守住的性质：API_KEY 未配置时全接口免鉴权，且写接口也不要求 X-Sequoia-Client。

    没有 cookie 就没有 CSRF 面，硬加守卫只会让 curl 脚本莫名 403（约束 §7 的兼容语义）。
    daily 被 patch，不外呼。
    """
    app, client = _build(tmp_path, api_key="")
    with client, patch(RUN_DAILY, return_value=fake_report()):
        assert client.get("/api/info").status_code == 200
        assert client.get("/api/strategies").status_code == 200
        assert client.get("/api/tasks").status_code == 200
        assert client.get("/api/signals", params={"date": "2026-01-01"}).status_code == 200
        # open 模式连"带错 key"都不拒——它压根不看头（T3 既有语义，别在 T7 改窄）
        assert client.get("/api/info", headers={"X-API-Key": "whatever"}).status_code == 200
        resp = client.post("/api/tasks/daily", json={})
        assert resp.status_code == 202
        app.state.manager.wait_for_completion(resp.json()["task_id"], timeout=15)
        assert client.get("/api/auth/me").json() == {"authenticated": True, "mode": "open"}


def test_open_mode_login_is_refused_with_explanation(tmp_path) -> None:
    """守住的性质：open 模式下 login 返回 400 auth_disabled 并说明无需登录。

    回 401 会让前端卡在"输入什么 key 都失败"的困惑里（T7 §4.3）。
    """
    _, client = _build(tmp_path, api_key="")
    with client:
        resp = _login(client, "")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "auth_disabled"
        assert "无需登录" in detail["hint"]
        # 有鉴权时错 key 是 401，open 时任何 key 都是 400——两态不能混
        assert _login(client, "any-key").json()["detail"]["code"] == "auth_disabled"


def test_open_mode_logout_still_204(tmp_path) -> None:
    """守住的性质：open 模式登出同样幂等（前端不必为无鉴权模式写分支）。"""
    _, client = _build(tmp_path, api_key="")
    with client:
        assert client.post("/api/auth/logout").status_code == 204


def test_info_exposes_auth_mode_sessions_and_frontend(tmp_path) -> None:
    """守住的性质：/api/info 新增三字段的值来自实况，不是常量（T7 §4.5）。"""
    _, client = _build(tmp_path, api_key="")
    with client:
        body = client.get("/api/info").json()
        assert body["auth_mode"] == "disabled"
        assert body["active_sessions"] == 0
        assert body["frontend_served"] is False
    app, client = _build(tmp_path)
    with client:
        _login_ok(client)
        body = client.get("/api/info", headers=AUTH).json()
        assert body["auth_mode"] == "apikey"
        assert body["active_sessions"] == 1
        assert body["frontend_served"] is False
        assert app.state.frontend_served is False


# ── SessionStore / authenticate 单元 ──


def test_session_store_issue_is_unique_and_valid() -> None:
    """守住的性质：token 由 secrets 生成、互不相同，且新签发的会话即刻可用。"""
    store = SessionStore()
    tokens = {store.issue(TTL) for _ in range(50)}
    assert len(tokens) == 50
    assert all(store.is_valid(t) for t in tokens)
    assert len(store) == 50


def test_session_store_is_valid_rejects_unknown_revoked_expired() -> None:
    """守住的性质：不存在 / 已撤销 / 已过期三种情形都必须 False（03 §3 的失效路径全集）。"""
    store = SessionStore()
    assert store.is_valid("nope") is False
    assert store.is_valid("") is False  # 空 token 不得与表里任何键相等
    live = store.issue(TTL)
    store.revoke(live)
    assert store.is_valid(live) is False
    stale = store.issue(TTL)
    store._expires_at[stale] = time.monotonic() - 1
    assert store.is_valid(stale) is False
    assert store.purge_expired() == 0  # 上一步 is_valid 已顺手回收


def test_session_store_revoke_unknown_is_silent() -> None:
    """守住的性质：撤销不存在的 token 不抛异常（登出必须幂等）。"""
    SessionStore().revoke("never-issued")


class _FakeRequest:
    """只提供 authenticate() 真正需要的 cookies 字段，不引入 TestClient 全链路。"""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        feishu_webhook_url="http://feishu.invalid/hook",
        db_path=str(tmp_path / "m.db"),
        task_db_path=str(tmp_path / "t.db"),
        api_key=API_KEY,
        session_ttl_seconds=TTL,
        serve_frontend=False,
    )


def test_authenticate_step_order_short_circuits_on_open_mode(tmp_path) -> None:
    """守住的性质：第 1 步（未配 API_KEY）优先于 cookie/header，坏凭据也放行。

    判定顺序是 03 §3.4 写死的契约，换序会让"无鉴权模式"依赖请求头内容。
    """
    settings = _settings(tmp_path).model_copy(update={"api_key": ""})
    ctx = authenticate(
        _FakeRequest({SESSION_COOKIE_NAME: "garbage"}), settings, SessionStore(), "wrong"
    )
    assert ctx.mode == "open"


def test_authenticate_prefers_valid_cookie_over_wrong_header(tmp_path) -> None:
    """守住的性质：cookie 有效时，即使 X-API-Key 是错的也放行（第 2 步先于第 3 步）。"""
    settings = _settings(tmp_path)
    store = SessionStore()
    token = store.issue(TTL)
    req = _FakeRequest({SESSION_COOKIE_NAME: token})
    assert authenticate(req, settings, store, "wrong").mode == "session"
    assert authenticate(req, settings, store).mode == "session"
    assert authenticate(req, settings, store, API_KEY).mode == "session"


def test_authenticate_falls_through_expired_cookie_to_header(tmp_path) -> None:
    """守住的性质：过期 cookie 不占用第 2 步，落到第 3 步按 header 判定（否则第 3 步形同虚设）。"""
    settings = _settings(tmp_path)
    store = SessionStore()
    token = store.issue(TTL)
    store._expires_at[token] = time.monotonic() - 1
    req = _FakeRequest({SESSION_COOKIE_NAME: token})
    assert authenticate(req, settings, store, API_KEY).mode == "apikey"
    with pytest.raises(HTTPException) as exc:
        authenticate(req, settings, store)
    assert exc.value.status_code == 401


def test_authenticate_rejects_unknown_cookie_and_wrong_header(tmp_path) -> None:
    """守住的性质：两个凭据都错 → 统一 401，且形态与"完全不带凭据"一致。"""
    settings = _settings(tmp_path)
    store = SessionStore()
    with pytest.raises(HTTPException) as with_junk:
        authenticate(_FakeRequest({SESSION_COOKIE_NAME: "junk"}), settings, store, "wr0ng")
    with pytest.raises(HTTPException) as anonymous:
        authenticate(_FakeRequest({}), settings, store)
    assert with_junk.value.status_code == anonymous.value.status_code == 401
    assert with_junk.value.detail == anonymous.value.detail == {"code": "unauthorized"}


def test_authenticate_error_body_has_no_credential_fragments(tmp_path) -> None:
    """守住的性质：401 的 detail 只有短码，不含 key、token、过期时刻（T7 §4.2）。"""
    settings = _settings(tmp_path)
    store = SessionStore()
    token = store.issue(TTL)
    req = _FakeRequest({SESSION_COOKIE_NAME: token[:-3] + "XXX"})
    with pytest.raises(HTTPException) as exc:
        authenticate(req, settings, store, "wr0ng-key")
    rendered = str(exc.value.detail)
    assert exc.value.detail == {"code": "unauthorized"}
    assert API_KEY not in rendered
    assert token not in rendered


def test_route_side_only_sees_require_auth() -> None:
    """守住的性质：require_api_key 与 require_auth 是同一个实例（T3 兼容不产生第二套判定）。

    两条依赖路径迟早分叉成"漏改一处即静默降级"，所以这里直接钉死对象同一性。
    """
    assert isinstance(require_auth, RequireAuth)
    assert require_api_key is require_auth


def test_no_module_level_session_store_in_api_package() -> None:
    """守住的性质：会话表只在 lifespan 里建（T3 约定，T7 §6 自查 3 的自动化版本）。

    模块级 SessionStore() 会让第二个测试 app 共用第一份会话表——表现是"某个
    不相关文件的用例突然带上了别人的登录态"，排查成本极高。
    """
    api_pkg = os.path.join(REPO_ROOT, "sequoia_x", "api")
    offenders: list[str] = []
    for dirpath, _dirs, files in os.walk(api_pkg):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if "SessionStore()" in stripped and not stripped.startswith("#"):
                        # 唯一合法出现处：lifespan 函数体内那行缩进赋值
                        if stripped != "app.state.sessions = SessionStore()":
                            offenders.append(f"{path}:{lineno}: {stripped}")
    assert offenders == []
