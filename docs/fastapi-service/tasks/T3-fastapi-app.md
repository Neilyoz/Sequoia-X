# T3 · FastAPI 应用与触发路由

**依赖**：T1、T2　**阻塞**：T4、T5
**规模**：7 个新文件 + 改 3 个文件 + 3 个测试文件 + 1 个启动脚本

## 1. 目标

建立 HTTP 层：应用工厂 + 生命周期 + API Key 鉴权 + 统一错误格式 +
系统信息与任务触发/查询路由。完成后 `uvicorn` 能把整个跑批能力暴露成异步接口。

本任务交付的路由：`GET /health`、`GET /api/info`、`GET /api/strategies`、
`POST /api/tasks/daily`、`POST /api/tasks/backfill`、`GET /api/tasks`、
`GET /api/tasks/{task_id}`。
（`/api/signals`、`/api/market/*` 是 T5，`/api/tasks/{id}/signals` 也是 T5。）

## 2. 动手前先读

- 约束 [§1 bootstrap](../01-constraints.md)、[§3 单飞](../01-constraints.md)、
  [§6 飞书副作用](../01-constraints.md)、[§7 配置兼容](../01-constraints.md)
- 架构 [§3.8 deps 约定](../02-architecture.md)、[§4 接口全表](../02-architecture.md)
- T2 的实际代码：`TaskManager.submit` 的异常类型、`TaskRecord.to_dict()` 字段名
- T1 的 `registry.py`：`get_all_strategies()` 返回的是**类**，`webhook_key` 是类属性，
  读它不需要实例化

## 3. 文件清单

新建：

| 文件 | 内容 |
| --- | --- |
| `sequoia_x/api/__init__.py` | docstring：`"""HTTP 服务层：FastAPI 应用、路由与依赖注入。"""` |
| `sequoia_x/api/app.py` | `create_app()`、`lifespan`、模块级 `app` |
| `sequoia_x/api/deps.py` | 鉴权依赖 + 从 `app.state` 取对象 |
| `sequoia_x/api/schemas.py` | 请求/响应模型 |
| `sequoia_x/api/routes/__init__.py` | docstring |
| `sequoia_x/api/routes/system.py` | `/health` `/api/info` `/api/strategies` |
| `sequoia_x/api/routes/tasks.py` | `/api/tasks*` |
| `run_server.py` | 开发用启动脚本 |
| `tests/test_api_system.py`、`tests/test_api_tasks.py`、`tests/test_api_auth.py` | 测试 |

修改：`sequoia_x/core/config.py`（新增服务字段）、`.env.example`、`pyproject.toml`（新依赖）+ `uv.lock`

禁止改动：`main.py`、`sequoia_x/runner/**`、`sequoia_x/task/**`（若发现接口不够用，
**汇报而不是改 T2 的代码**）、`sequoia_x/data/**`、`sequoia_x/strategy/**`、`sequoia_x/notify/**`、
`tests/test_main.py`。

## 4. 实现要点

### 4.1 配置新增（约束 §7）

```python
# Settings 追加，全部有默认值
api_host: str = "127.0.0.1"
api_port: int = 8000
api_key: str = ""
scheduler_enabled: bool = False        # T4 消费，本任务先声明
schedule_cron: str = "15 19 * * 1-5"   # T4 消费
timezone: str = "Asia/Shanghai"        # T2 已在用；若 T2 用环境变量读法，此处保持一致来源
```

- 若 T2 已经在 `models.py` 用 `os.environ.get("TIMEZONE", ...)`，不要强行统一成
  `Settings.timezone`（会引入反向依赖）。在 `config.py` 字段旁加一行注释指明
  "T2 的 `_now_iso` 直接从环境变量读，二者需保持同名配置"。
- `.env.example` 补齐并加中文注释，**必须写明两条风险**：
  `API_KEY` 留空 = 无鉴权，此时不要把 `API_HOST` 改成 `0.0.0.0`；
  `SCHEDULER_ENABLED=true` 会真的往飞书群推送。

### 4.2 `app.py`

```python
def create_app(settings: Settings | None = None) -> FastAPI: ...
```

- **最先** `from sequoia_x.core.bootstrap import bootstrap` 并在任何 `sequoia_x.*`
  业务 import 之前调用 `bootstrap()`（约束 §1）。位置：`app.py` 的模块顶层、
  其他 sequoia_x import 之上。ruff 的 isort 会把这段重排 ——
  **必须验证 `bootstrap()` 真的先于 `config`/`engine` 的 import 执行**；
  若被重排破坏，改用 `# noqa: E402` + 显式注释解释为什么这里的 import 顺序是刻意的。
  这是本任务最容易翻车的一点。
- `lifespan`（`@asynccontextmanager`）：
  - 进入：`store = TaskStore(settings.task_db_path)`；`store.init_schema()`；
    `n = store.recover_interrupted()`，`n > 0` 时 `logger.warning` 提示中断任务数；
    `manager = TaskManager(settings, store)`；`manager.start()`；
    `app.state.{settings, engine, store, manager}` 挂好；
    `scheduler` 占位为 `None`（T4 接线，本任务留注释指明接入点）。
  - 退出：`manager.shutdown(wait=False)`。
- `FastAPI(title="Sequoia-X 选股服务", version="3.0.0", lifespan=...)`。
  `version` 与 `pyproject.toml` 的 `project.version` 同步提升（当前 `2.0.0` → `3.0.0`，
  本次是形态变更，算 major）。
- 模块底部 `app = create_app()`，使 `uvicorn sequoia_x.api.app:app` 可用。
  这意味着 import 该模块会读 `.env` 并可能因缺 `FEISHU_WEBHOOK_URL` 抛
  `ValidationError` —— **测试必须用显式注入 `settings` 的 `create_app(settings)`**，
  不吃模块级 `app`。
- 注册全局异常处理：
  - `TaskAlreadyRunning` → 409，body `{"detail":{"code":"task_already_running",
    "task_id":..., "kind":...}}`
  - `UnknownStrategyError`（T1 定义）→ 400，body 含 `allowed` 列表
  - 未捕获 `Exception` → 500，`logger.exception`，body **不回显异常原文**
    （避免泄露路径信息），只给 `{"detail":{"code":"internal_error","task_id":null}}`
    形式 + 一条 `request_id`（uuid 短码，写进日志便于对账）。
- `API_KEY` 为空时启动打印一条 WARNING：
  `"未配置 API_KEY，服务将以无鉴权模式运行，请确保只监听回环地址"`。
- **不要**加 CORS 中间件（无前端需求，超范围）、不要加 gzip、不要加请求体大小限制之外的自定义。

### 4.3 `deps.py`

```python
ApiKeyHeader = Security(api_key_header)      # Header(alias="X-API-Key")

def require_api_key(request: Request, x_api_key: str | None = ApiKeyHeader) -> None:
    """settings.api_key 为空则直接放行；不匹配抛 401。"""

def get_settings_dep(request: Request) -> Settings
def get_manager(request: Request) -> TaskManager
def get_store(request: Request) -> TaskStore
```

- 鉴权用 `hmac.compare_digest` 常量时间比较，不要 `==`（虽然本地服务风险低，成本为零）。
- 401 统一 `detail={"code":"unauthorized"}`，**不区分 key 缺失与错误**。
- 路由级挂依赖：在 `tasks.py` / `system.py`（除 `/health`）的 `APIRouter(dependencies=[Depends(require_api_key)])`
  上声明，**不要每个函数各挂一遍**（漏一个就是安全洞）。
  `/health` 用独立 router 或在 `include_router` 时不带依赖，保证免鉴权。
- **为 T7 预留的架构要求**：T7 要把鉴权升级成"session cookie 或 API Key 二者之一"
  （见 [03 §3](../03-frontend-and-auth.md)）。因此本任务必须把**判定逻辑收敛进一个函数**
  `authenticate(...) -> AuthContext`，`require_api_key` 只是它的一个薄包装依赖。
  这样 T7 只需扩 `authenticate()`，不必重写各 router 的依赖声明。
  禁止在路由函数体内散落 `if x_api_key != settings.api_key` 这类内联比较。

### 4.4 `schemas.py`

```python
class DailyTaskRequest(BaseModel):
    push: bool = True
    strategies: list[str] | None = None      # 校验交给 registry.resolve，运行期 400

class TaskResponse(BaseModel):                # = TaskRecord.to_dict() 的显式模型
    task_id: str
    kind: str
    status: str
    triggered_by: str
    params: dict
    result: dict | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
```

响应模型用**显式字段**而不是 `dict`，这样 OpenAPI 文档才有意义。
`GET /api/tasks` 返回 `{"items": [...], "total": int, "limit": int, "offset": int}`。

### 4.5 路由行为

`POST /api/tasks/daily`
- 调 `manager.submit(TaskKind.DAILY, params={...}, triggered_by="api")` → **202** + TaskResponse。
- 冲突由 `TaskManager` 抛 `TaskAlreadyRunning`，全局 handler 转 409。路由函数里不要 try 它。
- 不做"参数预校验策略名"（`resolve` 在跑批时才执行）—— 但**要**在提交前用
  `registry.resolve(body.strategies)` 预检一次并让 `UnknownStrategyError` 冒到 400。
  理由：跑批要 2~3 分钟，用户不该等完才发现名字打错。

`POST /api/tasks/backfill`
- 无 body（或 `{}`）。`triggered_by="api"`，`params={}`。
- 文档里明确警告：回填约 12 分钟且会密集访问 baostock，**可能触发服务端限流**
  （约束 §3 提到的 `10001011`），不要脚本化反复调。

`GET /api/tasks`
- query：`kind`、`status`、`limit`（默认 50，`le=500`）、`offset`（`ge=0`）。
- 非法 `kind`/`status` → FastAPI 枚举校验自动 422。

`GET /api/tasks/{task_id}`
- 找不到 → 404，`detail={"code":"task_not_found","task_id":...}`。
- 返回 `result` 时**保留 `log_tail`**（它在 `result_json` 里，T2 放进去的）。

`GET /api/strategies`
- 遍历 `registry.get_all_strategies()`，输出
  `[{"name": cls.__name__, "webhook_key": cls.webhook_key,
     "has_dedicated_webhook": key in settings.strategy_webhooks}]`。
  `has_dedicated_webhook` 帮用户确认 `.env` 配对没配对。
- **不要实例化策略**（构造要 engine，且没必要）。

`GET /api/info`
- `{"name","version","timezone","scheduler_enabled","schedule_cron",
   "strategy_count","active_task_id"或 null,"db_path","started_at"}`。
  `started_at` 由 lifespan 记录在 `app.state`。

## 5. `run_server.py`

```python
"""开发用启动脚本：python run_server.py"""
from sequoia_x.core.bootstrap import bootstrap
bootstrap()
import uvicorn
from sequoia_x.core.config import get_settings

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run("sequoia_x.api.app:app", host=s.api_host, port=s.api_port, reload=False)
```

`reload=False` 写死并加注释：热重载会在 Windows 上以新进程重启，
正在跑的 baostock 任务会被硬切（约束 §4）。

## 6. 测试要求

用 `fastapi.testclient.TestClient(create_app(settings=假配置))`，
假 `Settings` 用 `Settings(_env_file=None, feishu_webhook_url="http://x", task_db_path=str(tmp_path/"t.db"), api_key="sekret")`
形式构造，**确保测试不吃仓库根 `.env`**（否则换台机器就红）。

`tests/test_api_auth.py`
- 无 key → 401；错 key → 401；`/health` 无 key → 200。
- `api_key=""` 配置下所有接口免鉴权可用。
- 401 响应体不含 key 的任何部分。

`tests/test_api_tasks.py`（patch `pipeline.run_daily`，绝不真跑批、绝不真推飞书）
- `POST /api/tasks/daily` → 202，`status` 为 `pending`/`running`，含 `task_id`。
- 跑批被 `threading.Event` 卡住期间再提交 → **409**，body `code=="task_already_running"`
  且 `task_id` 等于第一个任务。
- `strategies=["瞎写"]` → 400，响应含 `allowed`。
- `push=false` 透传到 `run_daily` 的调用参数（断言 mock 收到的 kwargs）。
- `GET /api/tasks` 分页与 `total`；`GET /api/tasks/{不存在}` → 404。
- 非法 `status=foo` → 422。

`tests/test_api_system.py`
- `/api/strategies` 长度 7，`webhook_key` 集合与注册表一致。
- `has_dedicated_webhook` 在只配了 `feishu_webhook_url` 时全为 `false`。
- `/api/info` 字段齐全。

**并发安全提示**：`TestClient` 是同步的，但 `TaskManager` 用真线程池。测试结束务必
`manager.shutdown()` 并等待，避免线程泄漏导致后续测试文件 flaky（`httpx` 关闭告警）。
必要时在 fixture 里等 `active_task()` 归 `None`。

## 7. 验收标准

```bash
ruff check sequoia_x run_server.py
pytest -q
uv run uvicorn sequoia_x.api.app:app --port 8123 &     # 或 .venv/Scripts/python.exe -m uvicorn ...
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8123/health          # 200
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8123/api/tasks       # 401（配了 key 时）
curl -s -H "X-API-Key: <key>" http://127.0.0.1:8123/docs | head -5           # OpenAPI 可访问
```

逐条自查：

1. `bootstrap()` 确实在 `config`/`engine` import 之前执行（加临时打印验证后删除）。
2. `/health` 不带 key 返回 200。
3. 触发路由**全部返回 202 且 1 秒内**返回，无一同步等待跑批。
4. `data/sequoia_v2.db` mtime 未变；`data/tasks.db` 已生成。
5. `git diff main.py` 为空（本任务不碰 CLI）。
6. 访问 `/docs` 能看到带 schema 的接口文档（不是裸 dict）。
7. `pytest -q` 在无网络、无 `FEISHU_WEBHOOK_URL` 环境下全绿
   （临时 `unset FEISHU_WEBHOOK_URL` 或在 CI 语义下验证）。

## 8. 完成后汇报

- 文件清单 + OpenAPI 截图或 `/openapi.json` 的路径列表
- 依赖新增内容与 `uv.lock` 是否已更新
- `bootstrap()` import 顺序问题的实际处理方式（有没有用 `noqa`）
- 需要 T4/T5 注意的接口变化
