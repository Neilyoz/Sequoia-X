"""系统信息路由：/health（免鉴权）+ /api/info + /api/strategies（鉴权）。

拆成两个 router 是刻意的：health_router 不含鉴权依赖，供探针/负载均衡直连
（架构 §4 鉴权列"否"）；其余接口整组挂在 require_api_key 上。
"""

from fastapi import APIRouter, Depends, Request

from sequoia_x.api.deps import require_api_key
from sequoia_x.api.schemas import HealthResponse, InfoResponse, StrategyInfo
from sequoia_x.runner.registry import get_all_strategies

health_router = APIRouter(tags=["system"])

router = APIRouter(tags=["system"], dependencies=[Depends(require_api_key)])


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
        "schedule_cron": settings.schedule_cron,
        "strategy_count": len(get_all_strategies()),
        "active_task_id": active.task_id if active else None,
        "db_path": settings.db_path,
        "started_at": request.app.state.started_at,
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
