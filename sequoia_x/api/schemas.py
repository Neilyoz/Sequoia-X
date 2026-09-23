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
    # 与 scheduler_enabled 不一致即为 cron 写错（build_scheduler 返回 None）——
    # 这是唯一的排查线索，T4 §4.5 要求两者必须都返回。
    scheduler_running: bool
    schedule_cron: str
    strategy_count: int
    active_task_id: str | None
    db_path: str
    started_at: str
    # T7 §4.5 三字段：排查"为什么我打不开页面/为什么不用登录"的第一现场信息。
    # auth_mode=disabled 即未配 API_KEY、全接口免鉴权；active_sessions 是未过期会话数，
    # 进程重启即归零（会话表在内存，03 §3）；frontend_served=false 就是根路径 404 的原因。
    auth_mode: str
    active_sessions: int
    frontend_served: bool


class HealthResponse(BaseModel):
    """GET /health 的存活探针响应。"""

    status: str


# ── T7 鉴权模型（03-frontend-and-auth.md §3）──


class LoginRequest(BaseModel):
    """POST /api/auth/login 的 body。

    刻意用 JSON body 而非 OAuth2PasswordRequestForm（表单）：后者会拖进
    python-multipart 这个新依赖，而本项目只有这一个凭据字段（03 §9）。
    """

    api_key: str = Field(
        ...,
        description=".env 里的 API_KEY。错了统一 401；未配 API_KEY（无鉴权模式）时该接口直接 400",
    )


class LoginResponse(BaseModel):
    """登录成功回执。cookie 在 Set-Cookie 头里，body 不含 token（前端不需要也不该拿到）。"""

    authenticated: bool
    expires_in: int = Field(..., description="会话有效期（秒），等于 SESSION_TTL_SECONDS")


class MeResponse(BaseModel):
    """GET /api/auth/me 的响应。

    mode 是前端路由的判据：open 直接跳过登录页（本地开发体验的关键），
    session/apikey 表示已鉴权，只是凭据形态不同。
    """

    authenticated: bool
    mode: str


# ── T5 查询接口响应模型（架构 §4 接口全表）──


class SignalItem(BaseModel):
    """一条选股信号（signal 表的对外视图，T5 §4.3）。

    name 来自行情库 stock_basic 批量补齐，未收录的代码为 null；
    xueqiu_code 与飞书卡片共用同一套 SH/SZ/BJ 映射，避免两处两种写法。
    """

    trade_date: str
    strategy: str
    webhook_key: str
    symbol: str
    name: str | None
    xueqiu_code: str


class SignalListResponse(BaseModel):
    """GET /api/signals 的分页信封；total 为过滤后的总条数。"""

    items: list[SignalItem]
    total: int
    limit: int
    offset: int


class TaskSignalsResponse(BaseModel):
    """GET /api/tasks/{id}/signals：单任务信号明细，不分页（最多几十条）。"""

    task_id: str
    items: list[SignalItem]
    total: int


class OhlcvItem(BaseModel):
    """一条日线行情；字段与 stock_daily 列一致（不含内部自增 id）。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class OhlcvResponse(BaseModel):
    """GET /api/market/{symbol}/ohlcv：items 按 date 升序（T5 §4.2）。"""

    symbol: str
    xueqiu_code: str
    items: list[OhlcvItem]
    total: int


class StockBasicResponse(BaseModel):
    """GET /api/market/{symbol}/basic：本地 stock_basic 未收录时 name 为 null。"""

    symbol: str
    name: str | None
    xueqiu_code: str
