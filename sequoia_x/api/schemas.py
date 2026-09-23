"""请求/响应 pydantic 模型。

响应一律用显式字段而非裸 dict：这样 /docs 的 OpenAPI schema 才有意义（T3 §4.4）。
字段名与 TaskRecord.to_dict() 的对外契约（架构 §3.4）逐一对应，不得擅改。
"""

from typing import Any

from pydantic import BaseModel, Field


class DailyTaskRequest(BaseModel):
    """POST /api/tasks/daily 的可选 body。

    strategies 的合法性校验交给 registry.resolve（提交前预检，未知名 → 400），
    这里不重复维护一份名单。
    """

    push: bool = Field(True, description="是否推送飞书；调试可传 false 只跑不推（约束 §6）")
    strategies: list[str] | None = Field(
        None, description="策略子集（类名或 webhook_key）；缺省为注册表全部"
    )


class TaskResponse(BaseModel):
    """TaskRecord.to_dict() 的显式镜像模型；status/kind 是对外契约枚举值。"""

    task_id: str
    kind: str
    status: str
    triggered_by: str
    params: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class TaskListResponse(BaseModel):
    """GET /api/tasks 的分页信封。"""

    items: list[TaskResponse]
    total: int
    limit: int
    offset: int


class StrategyInfo(BaseModel):
    """GET /api/strategies 的单个策略条目。"""

    name: str = Field(..., description="策略类名")
    webhook_key: str = Field(..., description="飞书专属 webhook 路由标识")
    has_dedicated_webhook: bool = Field(
        ..., description="该策略是否配了 STRATEGY_WEBHOOK_* 专属机器人（帮用户确认 .env）"
    )


class InfoResponse(BaseModel):
    """GET /api/info 的服务自述。active_task_id 为 null 表示当前无在跑任务。"""

    name: str
    version: str
    timezone: str
    scheduler_enabled: bool
    schedule_cron: str
    strategy_count: int
    active_task_id: str | None
    db_path: str
    started_at: str


class HealthResponse(BaseModel):
    """GET /health 的存活探针响应。"""

    status: str
