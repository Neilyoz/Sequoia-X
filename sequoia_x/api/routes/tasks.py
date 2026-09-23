"""任务触发与查询路由：/api/tasks*（全部要求鉴权，router 级声明）。

本文件的纪律（T3 §4.5 / 约束 §3）：
- 触发接口只调 TaskManager.submit 并立即返回 202，绝不等待跑批
  （wait_for_completion 在路由层禁用，那会把异步提交退化回同步阻塞）；
- 单飞冲突 TaskAlreadyRunning 不在这里 try——由 app.py 全局 handler 转 409；
- 策略名在提交前用 registry.resolve 预检：跑批要 2~3 分钟，用户不该等完
  才发现名字打错；未知名抛 UnknownStrategyError → 全局 handler 转 400。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sequoia_x.api.deps import get_manager, require_api_key
from sequoia_x.api.schemas import DailyTaskRequest, TaskListResponse, TaskResponse
from sequoia_x.runner.registry import resolve
from sequoia_x.task.models import TaskKind, TaskStatus

router = APIRouter(
    prefix="/api/tasks",
    tags=["tasks"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/daily", response_model=TaskResponse, status_code=202)
def submit_daily(body: DailyTaskRequest, request: Request) -> dict[str, object]:
    """触发日常跑批（同步快照 → 逐策略选股 → 可选推送），202 即返回。

    push 默认 true（与 CLI 行为一致，约束 §6）；调试请传 false 只跑不推。
    """
    resolve(body.strategies)  # 提交前预检，未知策略名冒到全局 handler 转 400
    manager = get_manager(request)
    record = manager.submit(
        TaskKind.DAILY,
        params={"push": body.push, "strategies": body.strategies},
        triggered_by="api",
    )
    return record.to_dict()


@router.post("/backfill", response_model=TaskResponse, status_code=202)
def submit_backfill(request: Request) -> dict[str, object]:
    """触发全市场历史回填（首次建库/补数据用）。

    警告：回填约 12 分钟且会密集访问 baostock，可能触发服务端限流
    （约束 §3 的 10001011，触发后本机 IP 一段时间不可恢复），
    不要脚本化反复调用。
    """
    manager = get_manager(request)
    record = manager.submit(TaskKind.BACKFILL, params={}, triggered_by="api")
    return record.to_dict()


@router.get("", response_model=TaskListResponse)
def list_tasks(
    request: Request,
    kind: TaskKind | None = Query(None, description="按任务类型过滤，非法值 422"),
    task_status: TaskStatus | None = Query(
        None, alias="status", description="按状态过滤，非法值 422"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    """历史任务列表（新→旧）。total 是过滤后的总条数，供分页。"""
    items, total = get_manager(request).list(
        kind=kind.value if kind else None,
        status=task_status.value if task_status else None,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [r.to_dict() for r in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, request: Request) -> dict[str, object]:
    """任务详情。result 里保留 log_tail（T2 在任务结束时写进 result_json）。"""
    record = get_manager(request).get(task_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail={"code": "task_not_found", "task_id": task_id}
        )
    return record.to_dict()
