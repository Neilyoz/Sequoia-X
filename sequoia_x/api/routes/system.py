"""系统信息路由：/health（免鉴权）+ /api/info + /api/strategies（鉴权）。

拆成两个 router 是刻意的：health_router 不含鉴权依赖，供探针/负载均衡直连
（架构 §4 鉴权列"否"）；其余接口整组挂在 require_auth 上。
"""

from fastapi import APIRouter, Depends, Request

from sequoia_x.api.deps import require_auth
from sequoia_x.api.schemas import HealthResponse, InfoResponse, StrategyInfo
from sequoia_x.runner.registry import get_all_strategies

health_router = APIRouter(tags=["system"])

router = APIRouter(tags=["system"], dependencies=[Depends(require_auth)])


@health_router.get("/health", response_model=HealthResponse)
def health() -> dict[str, str]:
    """存活探针：不碰任何业务对象，进程活着就返回 ok。"""
    return {"status": "ok"}


@router.get("/api/info", response_model=InfoResponse)
def info(request: Request) -> dict[str, object]:
    """服务自述：版本、时区、调度配置、策略数、当前活跃任务与关键路径。

    active_task_id 取 TaskManager.active_task()（内存优先、库兜底），
    与 409 响应报的是同一个任务 id，前端据此判断"是否有跑批在占单飞锁"。
    """
    settings = request.app.state.settings
    manager = request.app.state.manager
    active = manager.active_task()
    return {
        "name": request.app.title,
        "version": request.app.version,
        "timezone": settings.timezone,
        "scheduler_enabled": settings.scheduler_enabled,
        # 配置值与实际启动结果分开报：只起了一半（配置开但 cron 非法）时
        # 这两个字段不一致就是唯一的排查线索（T4 §4.5）。
        "scheduler_running": request.app.state.scheduler is not None,
        "schedule_cron": settings.schedule_cron,
        "strategy_count": len(get_all_strategies()),
        "active_task_id": active.task_id if active else None,
        "db_path": settings.db_path,
        "started_at": request.app.state.started_at,
        # T7 §4.5：这三项是"为什么打不开页面/为什么不用登录"的第一现场信息。
        # auth_mode 只报 disabled/apikey 两态——apikey 模式下"要不要登录"对调用方
        # 是同一个语义（要么 cookie 要么 X-API-Key），细分反而误导。
        "auth_mode": "apikey" if settings.api_key else "disabled",
        # active_sessions 是未过期会话数：进程重启即归零（会话表在内存里，03 §3）。
        "active_sessions": len(request.app.state.sessions),
        "frontend_served": request.app.state.frontend_served,
    }


@router.get("/api/strategies", response_model=list[StrategyInfo])
def strategies(request: Request) -> list[dict[str, object]]:
    """策略清单（注册表顺序）。只读类属性，不实例化——构造策略要 DataEngine，没必要。"""
    settings = request.app.state.settings
    return [
        {
            "name": cls.__name__,
            "webhook_key": cls.webhook_key,
            # strategy_webhooks 的键在 config 层已统一小写，这里对齐比较
            "has_dedicated_webhook": cls.webhook_key.lower() in settings.strategy_webhooks,
        }
        for cls in get_all_strategies()
    ]
