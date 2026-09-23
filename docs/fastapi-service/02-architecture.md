# 02 · 目标架构与接口契约

本文定义**改造后的分层、目录和跨任务共享的接口签名**。任务文件里的细节如与本文冲突，
以本文为准；两方都写不清楚时停下来汇报。

## 1. 分层

```
┌──────────────────────────────────────────────────────────────┐
│  入口层（三选一，都只是薄壳，不含业务逻辑）                     │
│  main.py            CLI：argparse → pipeline，管退出码         │
│  sequoia_x/api/app  HTTP：uvicorn，路由 → TaskManager          │
│  sequoia_x/scheduler 定时：cron → TaskManager                  │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│  编排层 sequoia_x/runner/   ← 唯一持有"跑批完整流程"的地方      │
│  纯同步函数，可被 CLI 直接调用、被 TaskManager 丢进线程池调用    │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│  任务层 sequoia_x/task/     单飞互斥、线程池、状态与历史持久化   │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│  领域层（已存在，本次基本不动）                                 │
│  strategy/*  7 个策略   ·  data/engine  SQLite+baostock        │
│  notify/feishu  推送    ·  core/{config,logger}               │
└──────────────────────────────────────────────────────────────┘
```

**依赖方向严格向下。** `runner/` 不得 import `api/` 或 `scheduler/`；
`task/` 不得 import `api/`；`strategy/` 与 `data/` 不得 import 任何上层。
这条用来保证 CLI 与 API 真等价，也方便后续单测。

## 2. 目录规划（★ 为本次新增）

```
Sequoia-X/
├── main.py                          # 改造：瘦身为 CLI 薄壳（约 40 行）
├── run_server.py                    ★ uvicorn 启动脚本（开发用，等价于 uvicorn 命令行）
├── pyproject.toml                   # 改造：加 fastapi/uvicorn/apscheduler
├── .env.example                     # 改造：补新配置项
├── data/
│   ├── sequoia_v2.db                # 行情库（不动）
│   └── tasks.db                     ★ 任务与选股结果库（运行时生成，gitignore）
├── docs/fastapi-service/            ★ 本目录
├── sequoia_x/
│   ├── core/
│   │   ├── config.py                # 改造：新增服务/调度/任务相关字段（全部带默认值）
│   │   ├── bootstrap.py             ★ 启动期副作用统一收口（dotenv + socket 超时）
│   │   └── logger.py                # 不动
│   ├── runner/                      ★ 编排层
│   │   ├── __init__.py
│   │   ├── registry.py              # 策略注册表，取代 main.py 的硬编码列表
│   │   └── pipeline.py              # run_daily() / run_backfill()
│   ├── task/                        ★ 任务层
│   │   ├── __init__.py
│   │   ├── models.py                # TaskKind / TaskStatus / TaskRecord / 结果模型
│   │   ├── store.py                 # tasks.db 读写（task_run + signal 表）
│   │   ├── logs.py                  ★ 按 task_id 归集日志的 Handler
│   │   └── manager.py               # TaskManager：单飞 + 线程池
│   ├── api/                         ★ HTTP 层
│   │   ├── __init__.py
│   │   ├── app.py                   # create_app() / lifespan / 中间件 / 异常处理 / 静态挂载
│   │   ├── auth.py                  ★ T7 会话表 + 鉴权判定唯一入口 authenticate()
│   │   ├── deps.py                  # 依赖注入：鉴权、Settings、TaskManager、DataEngine
│   │   ├── schemas.py               # 请求/响应 pydantic 模型
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── system.py            # /health /api/info /api/strategies
│   │       ├── auth.py              ★ T7 /api/auth/login|logout|me
│   │       ├── tasks.py             # /api/tasks/*
│   │       └── queries.py           # /api/signals /api/market/*
│   ├── scheduler/                   ★ 定时层
│   │   ├── __init__.py
│   │   └── jobs.py                  # AsyncIOScheduler 装配、cron 解析
│   ├── data/                        # 不动（可选新增只读查询方法，见 T5）
│   ├── notify/                      # 不动
│   └── strategy/                    # 不动
└── tests/
    ├── test_main.py                 # 不动（契约测试，改了就算违反 §2）
    ├── test_bootstrap.py            ★
    ├── test_runner_registry.py      ★
    ├── test_runner_pipeline.py      ★
    ├── test_task_store.py           ★
    ├── test_task_manager.py         ★
    ├── test_api_*.py                ★（按路由分文件）
    ├── test_api_auth_session.py     ★ T7
    ├── test_static_hosting.py       ★ T7
    └── test_scheduler.py            ★
```

### 2.1 前端工程（T8，独立 npm 项目）

```
frontend/                            ★ Next.js 16.3 + TS + Tailwind，只有 / 与 /login 两个路由
├── package.json                     # next@~16.3.0；package-lock.json 入库
├── next.config.ts                   # output:'export' / trailingSlash / images.unoptimized
│                                    #   rewrites 代理 /api → :8000（仅 next dev 生效）
├── .gitignore                       # node_modules/ .next/ out/
└── src/app/                         # layout.tsx · page.tsx · login/page.tsx · lib/ · components/
scripts/
├── build_frontend.sh / .ps1         ★ T8：npm ci + lint + tsc + build + 产物断言
└── smoke_test.sh   / .ps1           ★ T6：起服务打接口的冒烟验证
```

`frontend/out/`（构建产物）**不入库**，由脚本现场生成；生产环境由 FastAPI
`StaticFiles` 挂载在 `/`，与 API 同源同端口。详见 [03-frontend-and-auth.md](./03-frontend-and-auth.md)。

## 3. 共享接口契约（跨任务，签名不得擅改）

### 3.1 `sequoia_x/core/bootstrap.py`（T1）

```python
def bootstrap() -> None:
    """统一执行启动期副作用：加载 .env、设置全局 socket 超时。

    幂等。CLI 入口与 API 入口都必须在 import 任何 sequoia_x 业务子模块之前调用它。
    """
```

### 3.2 `sequoia_x/runner/registry.py`（T1）

```python
STRATEGY_CLASSES: tuple[type[BaseStrategy], ...]   # 顺序 = 原 main.py L70-78 的顺序
STRATEGY_KEYS: dict[str, type[BaseStrategy]]       # key 为类名小写 与 webhook_key 两种别名

def get_all_strategies() -> list[type[BaseStrategy]]
def resolve(names: list[str] | None) -> list[type[BaseStrategy]]
    """按类名或 webhook_key 解析子集；names 为 None 表示全部；含未知名称抛 UnknownStrategyError。
    保持注册表原始顺序输出，不要按用户传入顺序（保证跑批顺序稳定、结果可比）。"""
```

注册表顺序**必须**是：`MaVolumeStrategy, TurtleTradeStrategy, HighTightFlagStrategy,
LimitUpShakeoutStrategy, UptrendLimitDownStrategy, RpsBreakoutStrategy, PrivatePlacementStrategy`。
新增策略只在 `STRATEGY_CLASSES` 追加一行，取代原来"在 main.py 的 list 里追加"。

### 3.3 `sequoia_x/runner/pipeline.py`（T1）

```python
@dataclass
class StrategyOutcome:
    strategy_name: str
    webhook_key: str
    symbols: list[str]
    pushed: bool
    error: str | None = None          # 单策略异常不终止整批，记在这里

@dataclass
class DailyReport:
    trade_date: str                   # YYYY-MM-DD
    synced_rows: int
    outcomes: list[StrategyOutcome]
    push_enabled: bool

    @property
    def total_signals(self) -> int: ...

def run_daily(
    settings: Settings,
    *,
    push: bool = True,
    strategies: list[str] | None = None,
    use_multiprocessing: bool = True,
) -> DailyReport: ...

def run_backfill(settings: Settings) -> dict[str, Any]: ...
    # 返回 {"symbols_synced": int}，历史回填细节留在 engine 层打日志
```

不变量：

- 与现 `main.py:64-97` 逐步等价：`sync_today_bulk()` → 逐策略 `run()` → 有结果则
  `notifier.send(symbols, strategy_name, webhook_key)`，无结果记"跳过推送"日志。
- **日志文案原样保留**（`执行策略：{name}`、`{name} 选出 {n} 只股票`、
  `{name} 无选股结果，跳过推送`、`开始拉取最新快照...`、`快照同步完成，写入 {n} 只股票`）。
  用户靠这些文案判断跑批状态，改文案等于改 UI。
- 单个策略 `run()` 抛异常：捕获、记 `logger.exception`、把 `error` 写进该条
  `StrategyOutcome`，**继续跑后面的策略**。原代码没做到（整批挂掉），这是本次唯一
  刻意的行为改进，需在 T1 里显式标注并经需求方确认。
- 禁止 `sys.exit()` / `print()` / `get_settings()`（settings 由参数注入）。

### 3.4 `sequoia_x/task/models.py`（T2）

```python
class TaskKind(str, Enum):
    DAILY = "daily"
    BACKFILL = "backfill"

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool: ...

@dataclass
class TaskRecord:
    task_id: str            # uuid4 hex
    kind: TaskKind
    status: TaskStatus
    triggered_by: str       # "api" | "scheduler" | "cli"
    params: dict            # {"push": true, "strategies": null}
    result: dict | None     # DailyReport 的 dict 形式
    error: str | None
    created_at: str         # ISO8601，Asia/Shanghai
    started_at: str | None
    finished_at: str | None

class TaskAlreadyRunning(Exception):
    """单飞冲突。携带 running_task_id 与 kind，供路由层组装 409。"""
    def __init__(self, running_task_id: str, kind: TaskKind) -> None: ...
```

`status` 枚举值、`triggered_by` 取值集合是**对外契约**（写进响应 JSON），一旦定下不得随意改名。

### 3.5 `sequoia_x/task/store.py`（T2）

库：`Settings.task_db_path`（默认 `data/tasks.db`）。

```sql
CREATE TABLE IF NOT EXISTS task_run (
    task_id      TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    params_json  TEXT,
    result_json  TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS signal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    trade_date  TEXT NOT NULL,          -- 该信号对应的行情日期
    strategy    TEXT NOT NULL,          -- 策略类名
    webhook_key TEXT NOT NULL,
    symbol      TEXT NOT NULL,          -- 纯数字代码
    created_at  TEXT NOT NULL,
    UNIQUE (task_id, strategy, symbol)
);
CREATE INDEX IF NOT EXISTS idx_signal_date    ON signal (trade_date, strategy);
CREATE INDEX IF NOT EXISTS idx_signal_task    ON signal (task_id);
CREATE INDEX IF NOT EXISTS idx_task_run_state ON task_run (status, created_at);
```

```python
class TaskStore:
    def __init__(self, db_path: str) -> None: ...
    def init_schema(self) -> None
    def create(self, record: TaskRecord) -> None
    def update(self, record: TaskRecord) -> None            # 按 task_id upsert 全字段
    def get(self, task_id: str) -> TaskRecord | None
    def list(self, *, kind=None, status=None, limit=50, offset=0) -> tuple[list[TaskRecord], int]
    def active(self) -> TaskRecord | None                   # 非终态任务，单飞判定的兜底
    def save_signals(self, task_id, trade_date, outcomes) -> int
    def query_signals(self, *, trade_date=None, start=None, end=None,
                      strategy=None, symbol=None, limit, offset)
    def append_log_tail(self, task_id: str, lines: list[str]) -> None   # 见 3.6
    def prune(self, keep: int) -> None                      # 只保留最近 keep 条任务及其信号
    def recover_interrupted(self) -> int                    # 启动时把非终态标记为 failed
```

> 日志尾部**不是**单独存储的：由 `TaskManager` 在任务结束时塞进 `result_json.log_tail`，
> 所以 `TaskStore` 不需要 `append_log_tail` 之类的方法（早期草稿有，已删，避免实现出两套）。

服务启动时 `init_schema()`；将上次进程遗留的 `pending/running` 记录**一律标记为
`failed`，`error="进程重启导致任务中断"`**（否则重启后单飞锁会被脏数据永久占住）。

### 3.6 日志归集（T2）

`logs.py` 提供 `TaskLogCapture`：一个 `logging.Handler`，通过 `contextvars.ContextVar`
读取"当前 task_id"，把该任务的日志行缓冲到内存 deque，任务结束时写入
`task_run.result_json` 的 `log_tail` 字段（保留最近 `TASK_LOG_TAIL_LINES` 行，默认 200）。

- 只捕获 `sequoia_x.*` 命名空间的 logger，**不得**捕获 root 或 uvicorn 的日志。
- 不得改变 `core/logger.py` 现有的 rich 输出（用户仍要在终端看到实时日志）。
- 无 task_id 时（CLI 直调 pipeline）Handler 静默丢弃，不报错。

### 3.7 `sequoia_x/task/manager.py`（T2）

```python
class TaskManager:
    def __init__(self, settings, store, *, max_workers: int = 1) -> None
    def start(self) -> None            # 建线程池 + 恢复脏任务 + prune
    def shutdown(self, wait: bool = False) -> None
    def submit(self, kind: TaskKind, *, params: dict, triggered_by: str) -> TaskRecord
        # 单飞检查 → 建 PENDING 记录 → 提交线程池 → 立即返回（不等待）
        # 已有活跃任务时抛 TaskAlreadyRunning
    def get(self, task_id: str) -> TaskRecord | None
    def list(self, **filters) -> tuple[list[TaskRecord], int]
    def active_task(self) -> TaskRecord | None
    def wait_for_completion(self, task_id: str, timeout: float = 300) -> TaskRecord
        # 测试与调度器专用；HTTP 路由禁止调用（那会退化成同步阻塞）
```

- 线程池 `max_workers` **固定为 1**，参数保留是为了测试可注入。
- 执行体：`DAILY → runner.pipeline.run_daily(...)`，`BACKFILL → run_backfill(...)`。
- 异常 → `status=FAILED`，`error` 存 `f"{type(exc).__name__}: {exc}"` + traceback 尾部。
- **成功/失败都必须推进到终态**，任何路径遗留非终态记录都是缺陷（会让单飞永久卡死）。

### 3.8 `sequoia_x/api/deps.py` 单例持有（T3，T7 扩展）

`app.state` 上挂 `settings` / `engine` / `store` / `manager` / `scheduler` / `sessions`
（最后一个是 T7 的 `SessionStore`）。
`deps.py` 用 `fastapi.Depends` 从 `request.app.state` 取，**不使用模块级全局单例**
（否则测试无法构造第二个 app）。
鉴权判定统一走 `api/auth.py:authenticate()`，`deps.py` 只负责把它包成依赖
（禁止在 router 里内联比较 key —— 那会让 T7 的 session 支持变成到处补洞）。

## 4. HTTP 接口全表（T3/T4/T5 汇总）

| 方法 | 路径 | 鉴权 | 归属 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/health` | 否 | T3 | 存活探针，返回 `{"status":"ok"}` |
| POST | `/api/auth/login` | 否（本身是登录入口） | T7 | body `{"api_key":str}` → 下发 HttpOnly session cookie |
| POST | `/api/auth/logout` | cookie | T7 | 撤销会话并清 cookie，返回 204 |
| GET | `/api/auth/me` | cookie 或 key | T7 | 前端启动时判断登录态；返回 `mode`：`session`/`apikey`/`open` |
| GET | `/api/info` | 是 | T3 | 版本、时区、调度开关与 cron、策略数、活跃任务 |
| GET | `/api/strategies` | 是 | T3 | 策略清单：类名、`webhook_key`、是否已配专属 webhook |
| POST | `/api/tasks/daily` | 是 | T3 | 触发日常跑批，body `{push?:bool, strategies?:[str]}`，202 → TaskRecord |
| POST | `/api/tasks/backfill` | 是 | T3 | 触发历史回填，202 → TaskRecord |
| GET | `/api/tasks` | 是 | T3 | 历史列表，query `kind,status,limit,offset` |
| GET | `/api/tasks/{task_id}` | 是 | T3 | 详情，含 `result` 与 `log_tail` |
| GET | `/api/tasks/{id}/signals` | 是 | T5 | 该任务的选股结果 |
| GET | `/api/signals` | 是 | T5 | 跨任务选股结果，query `date`/`start`+`end`,`strategy,symbol,limit,offset` |
| GET | `/api/market/{symbol}/ohlcv` | 是 | T5 | 日线，query `start,end,limit` |
| GET | `/api/market/{symbol}/basic` | 是 | T5 | 股票名称等基础信息 |
| GET | `/`、`/login/` | 否 | T7 | Next.js 静态产物（`frontend/out`，目录不存在时不挂载） |

鉴权列的"是"= T7 之后接受 **session cookie 或 `X-API-Key` 任一**；
`API_KEY` 未配置时全部放行（开发语义）。判定逻辑唯一实现在 `api/auth.py:authenticate()`。
cookie 模式下的写操作额外要求 `X-Sequoia-Client: web` 头（CSRF 第二道防线，见 03 §4）。

响应约定：

- 触发类成功 **202 Accepted**（不是 200/201，语义是"已接受待执行"）。
- 单飞冲突 **409 Conflict**，`detail` 为 `{"code":"task_already_running","task_id":...,"kind":...}`。
- 未知路径/参数 **422**（FastAPI 默认）；未知策略名 **400**。
- 鉴权失败 **401**，`{"detail":{"code":"unauthorized"}}`，**不区分"key 不存在"与"key 不对"**；
  登录凭据错误 **401** `{"detail":{"code":"invalid_credentials"}}`（与前者区分，
  前端据此决定跳登录页还是提示重试）；cookie 模式写操作缺 `X-Sequoia-Client` → **403**
  `{"code":"missing_client_header"}`；登录暴力尝试超限 → **429**。
- 任务不存在 **404**。
- 所有时间字段 ISO8601 带 `+08:00` 偏移。

## 5. 配置项全表（新增，全部有默认值）

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `API_HOST` | `127.0.0.1` | 未配 `API_KEY` 时**必须**保持回环 |
| `API_PORT` | `8000` | |
| `API_KEY` | 空 | 空则不启用鉴权并打印 WARNING |
| `SCHEDULER_ENABLED` | `false` | 总开关，默认关（见约束 §6） |
| `SCHEDULE_CRON` | `15 19 * * 1-5` | 标准 5 段 cron |
| `TIMEZONE` | `Asia/Shanghai` | 调度与时间戳统一用它 |
| `TASK_DB_PATH` | `data/tasks.db` | 任务/信号库 |
| `TASK_HISTORY_LIMIT` | `200` | 超出的历史任务在启动时裁剪 |
| `TASK_LOG_TAIL_LINES` | `200` | 每任务保留的日志行数 |
| `SESSION_TTL_SECONDS` | `604800` | 登录会话有效期（7 天），T7 |
| `FRONTEND_DIST_PATH` | `frontend/out` | Next.js 静态导出产物目录，T7 |
| `SERVE_FRONTEND` | `true` | 关闭则只提供 API（T7） |

## 6. 依赖变更

`pyproject.toml` 主依赖新增：`fastapi`、`uvicorn[standard]`、`apscheduler>=3.10,<4`。
dev 可选组新增：`httpx`（TestClient 需要）、`pytest-asyncio`（若 T3 用到）。
**不引入** pydantic v1 兼容层、SQLAlchemy、alembic、celery、redis、`python-multipart`
（登录用 JSON body，不需要表单解析）。

前端依赖独立在 `frontend/package.json`，与 Python 侧无交叉：
`next@~16.3.0`、`react`、`typescript`、`tailwindcss`、`eslint-config-next`。
**不引入**组件库、图表库、状态管理库、第三方请求库（用原生 `fetch`）。

安装方式：Python 侧 `uv sync --extra dev`（本机 venv 为 Python 3.14）；
前端侧 `cd frontend && npm ci`（本机 Node 24.19 / npm 12）。
改完 `pyproject.toml` 必须同步 `uv.lock`；改完 `package.json` 必须同步
`package-lock.json`。两者都要一并提交。
**前端包管理器统一 npm，禁止混用 pnpm/yarn 造出第二份锁文件。**
