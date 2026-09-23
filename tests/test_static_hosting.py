"""T7 静态托管测试（T7 §5，03-frontend-and-auth.md §5.3）。

这里只验证"托管"这一件事：产物目录用 tmp_path 现造，绝不读写真实 data/ 下的库，
也不建 frontend/ 目录（那是 T8 的活）。

核心风险只有一个：``mount("/")`` 遮蔽后端路由。顺序写反的症状是"页面能开、接口全废"，
所以每个 API 路径都要断言"返回的不是 index.html"。
"""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sequoia_x.api.app import create_app
from sequoia_x.core.config import Settings

API_KEY = "sekret"
AUTH = {"X-API-Key": API_KEY}
ROOT_MARKER = "<h1>root index</h1>"
LOGIN_MARKER = "<h1>login index</h1>"


def _detach_task_log_captures() -> None:
    """摘除 lifespan 挂上的 TaskLogCapture（同 test_api_auth_session.py 的理由）。"""
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


@pytest.fixture
def caplog_app(caplog):
    """让 sequoia_x.api.app 的日志冒泡给 caplog（与 tests/test_scheduler.py 的 caplog_jobs 同法）。

    core/logger 建的业务 logger 全是 propagate=False（rich handler 直挂在自己身上），
    而 caplog 的捕获器在根 logger 上——不放开就永远收不到，断言会假红成"没打日志"。
    """
    from sequoia_x.api import app as app_module

    caplog.set_level(logging.INFO)
    app_module.logger.propagate = True
    yield caplog
    app_module.logger.propagate = False


def make_dist(tmp_path: Path) -> Path:
    """造一份最小 Next.js 导出产物：/ 与 /login/ 两个页面（03 §5.2 的路由集就这两个）。"""
    dist = tmp_path / "out"
    (dist / "login").mkdir(parents=True)
    (dist / "index.html").write_text(ROOT_MARKER, encoding="utf-8")
    (dist / "login" / "index.html").write_text(LOGIN_MARKER, encoding="utf-8")
    return dist


def build(tmp_path: Path, **overrides) -> TestClient:
    settings = Settings(
        _env_file=None,
        feishu_webhook_url="http://feishu.invalid/hook",
        # 行情库/任务库都指向 tmp 下不存在的路径：本文件只测挂载，一行真库都不碰（红线）
        db_path=str(tmp_path / "market_stub.db"),
        task_db_path=str(tmp_path / "tasks.db"),
        api_key=API_KEY,
        **overrides,
    )
    return TestClient(create_app(settings))


# ── 产物存在：页面与 API 共存 ──


def test_index_served_at_root(tmp_path) -> None:
    """守住的性质：GET / 返回 index.html 本体（03 §8 的同源访问入口）。"""
    client = build(tmp_path, frontend_dist_path=str(make_dist(tmp_path)))
    with client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert ROOT_MARKER in resp.text
    assert "text/html" in resp.headers["content-type"]


def test_directory_page_served_with_trailing_slash(tmp_path) -> None:
    """守住的性质：/login/ 命中 login/index.html —— T8 定 trailingSlash 的依据。"""
    client = build(tmp_path, frontend_dist_path=str(make_dist(tmp_path)))
    with client:
        resp = client.get("/login/", follow_redirects=False)
    assert resp.status_code == 200
    assert LOGIN_MARKER in resp.text


def test_bare_directory_path_redirects_to_trailing_slash(tmp_path) -> None:
    """实测结论：/login 由 StaticFiles 发 307 到 /login/，跟随后拿到页面。

    这意味着"导出产物是 login/index.html"与"trailingSlash: true"是配套关系：
    浏览器地址栏停在带斜杠的形态，刷新不会再经过一次跳转（03 §5.2）。
    """
    client = build(tmp_path, frontend_dist_path=str(make_dist(tmp_path)))
    with client:
        bare = client.get("/login", follow_redirects=False)
        followed = client.get("/login")
    assert bare.status_code == 307
    assert bare.headers["location"].endswith("/login/")
    assert followed.status_code == 200
    assert LOGIN_MARKER in followed.text


def test_api_paths_are_not_shadowed_by_root_mount(tmp_path) -> None:
    """守住的性质（本文件的核心）：mount("/") 之后 /health、/api/*、/docs、
    /openapi.json 仍归后端，响应对不上 HTML 才算没被吃掉。

    断言写成"不是 index.html"而不是只看状态码：StaticFiles 的 404 也会带 html，
    只看 status 会把"被遮蔽后 404"误判成通过。
    """
    client = build(tmp_path, frontend_dist_path=str(make_dist(tmp_path)))
    with client:
        health = client.get("/health")
        info = client.get("/api/info", headers=AUTH)
        tasks = client.get("/api/tasks", headers=AUTH)
        docs = client.get("/docs")
        openapi = client.get("/openapi.json")
        # 未鉴权的 API 路径必须仍是后端的 401 JSON，而不是页面的 404
        shadowed = client.get("/api/tasks", follow_redirects=False)
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert info.status_code == 200 and info.json()["auth_mode"] == "apikey"
    assert tasks.status_code == 200 and "items" in tasks.json()
    assert docs.status_code == 200 and "swagger-ui" in docs.text.lower()
    assert openapi.status_code == 200
    assert isinstance(openapi.json(), dict) and openapi.json()["openapi"].startswith("3")
    assert shadowed.status_code == 401
    assert shadowed.json()["detail"] == {"code": "unauthorized"}
    for resp in (health, info, tasks, docs, openapi, shadowed):
        assert ROOT_MARKER not in resp.text


def test_frontend_served_flag_reflects_actual_mount(tmp_path) -> None:
    """守住的性质：/api/info 的 frontend_served 与真挂了产物一致（排障时的第一现场信息）。"""
    client = build(tmp_path, frontend_dist_path=str(make_dist(tmp_path)))
    with client:
        assert client.get("/api/info", headers=AUTH).json()["frontend_served"] is True


def test_unknown_path_is_404_not_spa_fallback(tmp_path) -> None:
    """守住的性质：未导出路径就是 404（03 §5.3 明确拒绝 catch-all 回退）。

    回退到 index.html 会让"真 404"和"前端 404"混在一起，还会把接口路径打错的
    请求伪装成正常页面。
    """
    client = build(tmp_path, frontend_dist_path=str(make_dist(tmp_path)))
    with client:
        resp = client.get("/signals")
    assert resp.status_code == 404
    assert ROOT_MARKER not in resp.text


# ── 产物缺失 / 显式关闭：自动降级为"只提供 API" ──


def test_missing_dist_degrades_to_api_only(tmp_path, caplog_app) -> None:
    """守住的性质：产物不存在时服务照常起、API 全通、根路径 404，且日志给出下一步动作。

    这是 T8 之前的常态，也是"根路径为什么 404"唯一的线索来源——StaticFiles 压根没挂，
    所以 WARNING 文案必须带构建指令（T7 §4.4）。
    """
    missing = tmp_path / "nope" / "out"
    client = build(tmp_path, frontend_dist_path=str(missing))
    with client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/tasks", headers=AUTH).status_code == 200
        root = client.get("/")
        info = client.get("/api/info", headers=AUTH).json()
    assert root.status_code == 404
    assert info["frontend_served"] is False
    warning = next(r for r in caplog_app.records if r.levelno >= logging.WARNING)
    assert "nope" in warning.message  # 带上找不到的具体路径
    assert "cd frontend && npm run build" in warning.message


def test_serve_frontend_false_skips_mount_even_with_dist(tmp_path, caplog_app) -> None:
    """守住的性质：SERVE_FRONTEND=false 时即使产物在也不挂（只跑 API 的部署形态）。"""
    dist = make_dist(tmp_path)
    client = build(tmp_path, frontend_dist_path=str(dist), serve_frontend=False)
    with client:
        assert client.get("/").status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/api/info", headers=AUTH).json()["frontend_served"] is False
    assert any("SERVE_FRONTEND" in r.message for r in caplog_app.records)


def test_dist_path_is_resolved_from_settings_not_cwd(tmp_path) -> None:
    """守住的性质：frontend_dist_path 指哪儿挂哪儿（测试不依赖仓库根有 frontend/ 目录）。

    顺带钉住"绝不创建 frontend/"这条红线：仓库根若被某个用例造出 frontend/out，
    默认配置的用例就会静默挂上别人的产物，这里用显式路径堵住这条路。
    """
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "index.html").write_text("<h1>elsewhere</h1>", encoding="utf-8")
    client = build(tmp_path, frontend_dist_path=str(other))
    with client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "elsewhere" in resp.text


def test_static_files_do_not_require_auth(tmp_path) -> None:
    """守住的性质：静态资源不鉴权（否则未登录连登录页都打不开，形成死锁，T7 §4.4）。"""
    client = build(tmp_path, frontend_dist_path=str(make_dist(tmp_path)))
    with client:
        assert client.get("/login/").status_code == 200  # 不带任何凭据
        assert client.get("/").status_code == 200


def test_default_settings_do_not_mount_repo_frontend(tmp_path) -> None:
    """守住的性质：默认 frontend/out 不存在时降级为只提供 API（T8 之前仓库里没有 frontend/）。

    同时验证仓库根确实还没被建出 frontend/ —— 本任务的红线之一。
    """
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / "frontend").exists(), "T7 不得创建 frontend/ 目录（那是 T8 的活）"
    client = build(tmp_path, frontend_dist_path=str(repo_root / "frontend" / "out"))
    with client:
        assert client.get("/").status_code == 404
        assert json.loads(client.get("/health").content) == {"status": "ok"}
