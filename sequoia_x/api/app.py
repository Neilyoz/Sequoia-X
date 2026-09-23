"""FastAPI 应用工厂与生命周期：uvicorn sequoia_x.api.app:app 的服务态入口。

与 CLI（main.py）共用 runner/task 两层，是唯一允许 import 上层的入口壳（架构 §1）。
"""

from sequoia_x.core.bootstrap import bootstrap

# bootstrap() 必须先于一切 sequoia_x 业务子模块 import（约束 §1：.env 要先于
# Settings 实例化、socket 超时要先于 baostock/akshare 建连；uvicorn 导入 app 的
# 路径与 CLI 完全不同，不收口就会出现"CLI 能跑、API 跑不了"的分裂故障）。
# 下方 import 刻意位于语句之后，noqa: E402 标记这是有意的顺序（与 main.py 同手法），
# 防止 ruff/编辑器把 import 块重排到 bootstrap() 之前。
bootstrap()

from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from sequoia_x.api.auth import LoginRateLimiter, SessionStore  # noqa: E402
from sequoia_x.api.routes import auth, queries, system, tasks  # noqa: E402
from sequoia_x.core.config import Settings, get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.runner.registry import STRATEGY_KEYS, UnknownStrategyError  # noqa: E402
from sequoia_x.scheduler.jobs import build_scheduler  # noqa: E402
from sequoia_x.task.manager import TaskManager  # noqa: E402
from sequoia_x.task.models import TaskAlreadyRunning, _now_iso  # noqa: E402
from sequoia_x.task.store import TaskStore  # noqa: E402

logger = get_logger(__name__)

# 与 pyproject 的 project.version 同步提升：2.x 仅 CLI，3.0 起为常驻 HTTP 服务（T3 §4.2）。
APP_VERSION = "3.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：建任务库 → 恢复脏任务 → 起单飞管理器 → 挂 app.state；退出：关池不杀任务。"""
    settings: Settings = app.state.settings

    store = TaskStore(settings.task_db_path)
    store.init_schema()
    # 先于 manager.start() 恢复并如实播报条数：上次进程崩溃/强杀遗留的 pending/running
    # 记录若不标记，store.active() 兜底会把单飞锁永久占住（架构 §3.5）。
    recovered = store.recover_interrupted()
    if recovered:
        logger.warning(f"启动恢复：{recovered} 条上次进程遗留的中断任务已标记为失败")

    manager = TaskManager(settings, store)
    manager.start()
    # DataEngine 只建表不写库（CREATE TABLE IF NOT EXISTS 对既有库是 mtime 无操作），
    # T3 路由不直接用它，挂上是为 T5 查询路由接线（架构 §3.8）。
    engine = DataEngine(settings)

    app.state.store = store
    app.state.manager = manager
    app.state.engine = engine
    app.state.started_at = _now_iso()
    # 会话表与登录限流计数器挂在 app.state 而不是模块级：单进程部署下二者本就只有一份，
    # 但测试要能构造互不污染的第二个 app（T3 起确立的约定，架构 §3.8）。
    # 03 §3 说的"启动时顺手 purge_expired"在这里是空操作——表随进程新建必然为空，
    # 真正的回收时机是每次登录（见 routes/auth.py）。
    app.state.sessions = SessionStore()
    app.state.login_limiter = LoginRateLimiter()

    if not settings.api_key:
        logger.warning("未配置 API_KEY，服务将以无鉴权模式运行，请确保只监听回环地址")

    # T4 调度器：回调只经 TaskManager.submit 走单飞锁（约束 §3），cron 非法或
    # 未启用时 build_scheduler 返回 None，服务照常启动（scheduler_running 如实反映）。
    scheduler = build_scheduler(settings, manager)
    if scheduler is not None:
        scheduler.start()
        logger.info(f"定时调度已启动：{settings.schedule_cron} ({settings.timezone})")
    app.state.scheduler = scheduler
    yield

    # 退出期异常不该刷屏（任务在跑完前不硬切，shutdown 只停调度线程）。
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass

    # wait=False：正在跑的 baostock 任务被硬切会留下脏 socket，比等它跑完更糟（T2 语义）。
    manager.shutdown(wait=False)


def _register_exception_handlers(app: FastAPI) -> None:
    """全局异常 → 统一 JSON 形态（架构 §4 响应约定），路由体内不再各自处理。"""

    @app.exception_handler(TaskAlreadyRunning)
    async def _task_already_running(request: Request, exc: TaskAlreadyRunning) -> JSONResponse:
        # 与 GET /api/info 的 active_task_id 同源：报的就是占锁那个任务
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "task_already_running",
                    "task_id": exc.running_task_id,
                    "kind": exc.kind.value,
                }
            },
        )

    @app.exception_handler(UnknownStrategyError)
    async def _unknown_strategy(request: Request, exc: UnknownStrategyError) -> JSONResponse:
        # allowed 用注册表全量别名（类名/下划线名/webhook_key），异常消息里是顿号
        # 拼接的人读文本，机器可读列表在这里组装
        return JSONResponse(
            status_code=400,
            content={"detail": {"code": "unknown_strategy", "allowed": sorted(STRATEGY_KEYS)}},
        )

    @app.exception_handler(Exception)
    async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
        # 不回显异常原文与路径细节：detail 只带短码 request_id，完整 traceback 进日志对账。
        request_id = uuid4().hex[:8]
        logger.exception(
            f"未处理异常（request_id={request_id}）：{request.method} {request.url.path}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "code": "internal_error",
                    "task_id": None,
                    "request_id": request_id,
                }
            },
        )


def mount_frontend(app: FastAPI, settings: Settings) -> None:
    """把 Next.js 静态导出产物挂到根路径。找不到目录时只警告，不报错。

    调用位置是硬要求：**必须在所有 include_router 与异常处理器注册之后**。
    Starlette 按注册顺序匹配路由，先挂 `mount("/")` 会把 /api/*、/health、/docs、
    /openapi.json 全部吃掉，变成全站 404。这条顺序写反的后果不可见（页面还能开，
    接口全废），所以 tests/test_static_hosting.py 有专门断言，别只靠这段注释。

    静态资源不加鉴权：页面本身无信息量（index.html 不含数据），放开反而避免了
    "未登录连登录页都打不开"的死锁（T7 §4.4）。
    """
    if not settings.serve_frontend:
        app.state.frontend_served = False
        logger.info("SERVE_FRONTEND=false，仅提供 API 服务")
        return
    dist = Path(settings.frontend_dist_path)
    if not dist.is_dir():
        # 目录不存在是 T8 之前的常态，也是"根路径为什么 404"的唯一线索，
        # 因此 WARNING 里必须带上后续动作。
        app.state.frontend_served = False
        logger.warning(
            f"未找到前端产物 {dist}，仅提供 API 服务（开发期正常）；"
            "如需页面，执行 cd frontend && npm run build"
        )
        return
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    app.state.frontend_served = True
    logger.info(f"前端静态资源已挂载：{dist}")


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。

    测试必须显式注入假 Settings（``Settings(_env_file=None, ...)``）走这里，
    不要用模块级 ``app``——后者 import 时就读真实 .env（T3 §4.2/§6）。
    """
    if settings is None:
        settings = get_settings()
    app = FastAPI(title="Sequoia-X 选股服务", version=APP_VERSION, lifespan=lifespan)
    # lifespan 之前先挂 settings：lifespan 与鉴权依赖都从 app.state 读它
    app.state.settings = settings
    app.state.engine = None  # lifespan 填真实例（架构 §3.8 的对象清单）
    app.state.store = None
    app.state.manager = None
    app.state.scheduler = None  # 真实调度器由 lifespan 装配（T4）
    app.state.sessions = None  # 会话表由 lifespan 装配（T7）
    app.state.login_limiter = None  # 登录限流计数器由 lifespan 装配（T7）
    app.state.frontend_served = False  # mount_frontend 覆盖：装配时机在 create_app 而非 lifespan
    _register_exception_handlers(app)
    # /health 独立 router：探针免鉴权（架构 §4）
    app.include_router(system.health_router)
    app.include_router(system.router)
    app.include_router(tasks.router)
    app.include_router(queries.router)
    # 鉴权路由不挂 router 级依赖（三个端点的鉴权要求各不相同），但必须排在
    # require_auth 保护的 router 之后——顺序本身无副作用，只是让"最后才是通配挂载"
    # 这条规则在代码里一眼可读。
    app.include_router(auth.router)
    # ★ 通配挂载放在最后：见 mount_frontend 的 docstring
    mount_frontend(app, settings)
    return app


# 供 `uvicorn sequoia_x.api.app:app` 使用；import 本模块会读 .env 并可能因缺
# FEISHU_WEBHOOK_URL 抛 ValidationError —— 这是服务入口的预期行为，测试勿用。
app = create_app()
