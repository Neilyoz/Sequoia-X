# Sequoia-X 服务化改造（CLI → FastAPI + 前端）工作细则

> 本目录是本次改造的**唯一事实来源**。所有执行 agent 必须先完整阅读
> [01-constraints.md](./01-constraints.md) 与 [02-architecture.md](./02-architecture.md)
> （前端与登录鉴权任务另需 [03-frontend-and-auth.md](./03-frontend-and-auth.md)），
> 再阅读自己被指派的任务文件，然后动手。文档与你的直觉冲突时，**以文档为准**；
> 文档与现有代码事实冲突时，**停下来汇报，不要自行改设计**。

## 1. 目标与已确认的产品决策

把项目从"只能 `python main.py` 手动跑批"改造成**常驻 FastAPI 服务 + 浏览器看板**，同时保留 CLI。

以下决策已与需求方确认，不再讨论（后四项为追加范围）：

| 决策点 | 结论 |
| --- | --- |
| CLI 去留 | **双形态共存**。`python main.py` / `--backfill` 行为逐字节不变，CLI 与 API 共用同一编排层 |
| 定时调度 | **内置 APScheduler + API 手动触发**，可通过 `.env` 整体开关 |
| 长任务处理 | **异步后台任务 + 状态查询**。提交即返回 `task_id`，全局单飞互斥 |
| 交付范围 | **服务化核心 + 运维配套**：触发/状态/选股结果/行情查询接口 + API Key 鉴权 + 测试 + 文档 |
| 前端技术栈 | **Next.js 16.3 + TypeScript + Tailwind**，工程目录 **`frontend/`**（追加需求） |
| 前端部署 | **`output: 'export'` 静态导出**，由 FastAPI `StaticFiles` 挂载，**与 API 同源同端口** |
| 前端范围 | **只有一个选股信号看板**（+ 必需的登录页）。不做仪表盘、不做在线触发、不做 K 线图 |
| 浏览器鉴权 | **登录页输 API Key 换 HttpOnly session cookie**；`/api/*` 接受 cookie 或 `X-API-Key` 任一 |

追加范围的设计与坑位集中在 [03-frontend-and-auth.md](./03-frontend-and-auth.md)，
前端任务实现细则见 [tasks/T7](./tasks/T7-session-auth-static.md)（后端鉴权+托管）与
[tasks/T8](./tasks/T8-nextjs-signals-board.md)（看板页面）。

明确**不做**（超出已确认范围）：

- 策略参数的在线增删改、策略热加载
- 仪表盘 / 任务详情页 / 在线触发按钮（触发仍走 API、CLI 或定时调度）
- 多 worker / 分布式 / 队列中间件（Redis、Celery）
- 把 `strategy.run()` 改造成 `async def`
- 用户名密码体系、OAuth、多用户、审计日志
- 前端组件库、图表库、Playwright 等前端测试框架、SSR / Server Actions

## 2. 任务拆分与依赖顺序

```
T1 runner 编排层 ──┬── T2 任务管理 ────┬── T3 FastAPI 应用 ──┬── T4 定时调度
                   │                   │                     │
                   └───────────────────┴── T5 查询接口 ──────┘
                                                │
                          T7 session 鉴权 + 静态托管（后端） ◄┘
                                                │
                          T8 Next.js 信号看板（frontend/）
                                                │
                          T1..T5,T7,T8 全部完成 ─┴── T6 测试/依赖/文档收口
```

**执行顺序**：T1 → T2 → T3 → T4 → T5 → T7 → T8 → T6。

（T6 排在最后是因为它是收口：要写跨层集成测试、要核对前端产物、要重写 README 的部署章节。
编号保持 T4/T5 不变是为了让已下发的派单提示词不失效。）

原因见 [01-constraints.md §0](./01-constraints.md)。简言之：这些任务改的是同一条调用链，
且 T3 的路由要 import T2 的 `TaskManager`。**禁止并行派单**，除非两个任务的文件集合完全不相交
（例如 T4 与 T5 在 T3 合入后可并行，但两者都要独占自己的测试文件；
T7 与 T8 **不可**并行，T8 依赖 T7 的登录契约）。

| 任务 | 文件 | 产出 | 依赖 |
| --- | --- | --- | --- |
| T1 | [tasks/T1-runner-layer.md](./tasks/T1-runner-layer.md) | `sequoia_x/runner/`、`core/bootstrap.py`、瘦身后的 `main.py` | — |
| T2 | [tasks/T2-task-system.md](./tasks/T2-task-system.md) | `sequoia_x/task/`（模型/存储/管理器） | T1 |
| T3 | [tasks/T3-fastapi-app.md](./tasks/T3-fastapi-app.md) | `sequoia_x/api/` 应用与触发路由 | T2 |
| T4 | [tasks/T4-scheduler.md](./tasks/T4-scheduler.md) | `sequoia_x/scheduler/` 与 lifespan 接线 | T3 |
| T5 | [tasks/T5-query-apis.md](./tasks/T5-query-apis.md) | 选股结果 / 行情 / 策略清单查询路由 | T3 |
| T7 | [tasks/T7-session-auth-static.md](./tasks/T7-session-auth-static.md) | `api/auth.py`、`/api/auth/*`、前端静态挂载 | T3、T5 |
| T8 | [tasks/T8-nextjs-signals-board.md](./tasks/T8-nextjs-signals-board.md) | `frontend/` Next.js 信号看板 | T7 |
| T6 | [tasks/T6-tests-docs-deps.md](./tasks/T6-tests-docs-deps.md) | 测试补全、依赖锁定、README 与部署文档 | T1..T5、T7、T8 |

## 3. 派单方式（给开发经理用）

给每个 agent 的提示词模板，按 `{{}}` 填充后直接下发：

```
你是 Sequoia-X 项目的实现工程师。项目根目录 D:\quant_stock\Sequoia-X。

任务：完成 docs/fastapi-service/tasks/{{T3-fastapi-app.md}} 中定义的全部内容。

必读（按顺序，全文读完再动手）：
1. docs/fastapi-service/01-constraints.md   ← 全局硬约束，违反即返工
2. docs/fastapi-service/02-architecture.md  ← 目标架构与目录规划
3. docs/fastapi-service/03-frontend-and-auth.md ← 仅 T7/T8 需要（前端与登录鉴权）
4. docs/fastapi-service/tasks/{{T3-fastapi-app.md}}  ← 本次任务
5. 已完成任务 {{T1、T2}} 的实际产出代码（以代码为准，不要只看文档）

要求：
- 严格按任务文件的"文件清单"创建/修改文件，不要触碰"禁止改动"里的文件
- 逐条通过任务文件末尾的"验收标准"，自己在最后跑一遍验收命令
- 不写超出任务范围的功能；有更好想法先汇报，不要自作主张实现
- 全部注释、日志、文档用中文

完成后汇报：改了哪些文件、验收命令的实际输出、未尽事项、你发现的文档与代码不一致之处。
```

## 4.  Definition of Done（整体验收）

改造完成当且仅当以下**全部**成立：

1. `pytest -q` 全绿，且 `tests/test_main.py` 的"异常 → 非零退出码"属性测试**未被修改仍然通过**。
2. `python main.py` 与改造前的日常模式行为一致（同步数据 → 跑 7 个策略 → 有结果则推送飞书），
   `python main.py --backfill` 仍走回填分支后 `return`。
3. `uvicorn sequoia_x.api.app:app --port 8000` 可启动，`GET /health` 返回 200。
4. 配置 `API_KEY` 后，既无 session cookie 又无 `X-API-Key` 头的 `/api/*` 请求返回 401；
   `/health` 仍免鉴权。用正确 key 调 `POST /api/auth/login` 能拿到
   `HttpOnly; SameSite=strict` 的 cookie，且带着它访问 `/api/tasks` 返回 200。
5. `POST /api/tasks/daily` 在 1 秒内返回 `task_id` 与 `status=pending|running`，不阻塞等待跑批结束。
6. 跑批进行中再次 `POST /api/tasks/daily` 返回 **409**，响应体带上正在运行的 `task_id`。
7. `GET /api/tasks/{id}` 在任务结束后能看到 `status=success`、耗时、每个策略的选股数量；
   `GET /api/tasks/{id}/signals` 能取到落库的具体股票代码。
8. `SCHEDULER_ENABLED=true` 且 `SCHEDULE_CRON="*/2 * * * *"` 时，服务启动后 2 分钟内
   能看到调度触发的任务记录（联调用配置，非生产默认）。
9. `SCHEDULER_ENABLED` 默认值为 `false` —— 新人 clone 后启动服务不会误推送到飞书群。
10. 新增依赖（`fastapi`、`uvicorn[standard]`、`apscheduler`）已进入 `pyproject.toml` 并同步 `uv.lock`。
11. `.env.example` 含全部新增配置项及中文注释；README 有"作为服务运行"章节。
12. `data/sequoia_v2.db`（约 456 MB 行情库）**内容未被本次改造改动或清空**。
13. `cd frontend && npm ci && npm run build` 成功产出 `frontend/out/`，
    且 `frontend/out/` **未被 git 跟踪**、`package-lock.json` **已提交**。
14. 构建后访问 `http://127.0.0.1:8000/` 能看到登录页，输入 `.env` 里的 `API_KEY`
    登录后**看到真实信号数据**；此时 `/health`、`/docs`、`/api/*` 均未被静态挂载遮蔽。
15. 刷新 `/login/` 与 `/` 都不 404；浏览器 Network 面板**无任何指向 :3000 的请求**。
16. API Key 不出现在 `localStorage` / `sessionStorage` / cookie 中
    （`grep -rn "localStorage\|sessionStorage" frontend/src` 无结果）。
17. 看板在加载 / 空结果 / 后端错误 / 未回填四种状态下都有明确中文文案，无白屏与死转圈。

以上 17 条构成整体验收口径，T6 的汇报必须逐条给结论与证据。

## 5. 风险登记册（执行时反复回看）

| 风险 | 严重度 | 处置 |
| --- | --- | --- |
| baostock 使用**进程级全局 socket**，并发跑批会互相踩连接并触发服务端限流 `10001011` | 高 | 全局单飞互斥 + 单 worker 线程池，见约束 §3 |
| `sync_today_bulk()` 内部用 `multiprocessing.Pool`，在工作线程里创建子进程有平台风险 | 高 | 约束 §4，T2 必须实测；失败则退化为串行同步 |
| `load_dotenv()` / `socket.setdefaulttimeout(60)` 的**调用时机**被破坏 | 高 | 约束 §1，统一收口到 `bootstrap()` |
| 破坏 `tests/test_main.py` 对 `main.get_settings` 的 patch 契约 | 中 | 约束 §2，T1 验收强制跑该测试 |
| 富文本日志（rich）在后台线程并发写 stdout 交错 | 低 | 日志按 `task_id` 归集，见 T2 |
| API 暴露后被人乱调 backfill 打爆 baostock | 中 | 鉴权 + 单飞 + 默认只监听 `127.0.0.1` |
| 任务历史无限增长 | 低 | `TASK_HISTORY_LIMIT` 裁剪，只裁 `task_run/signal`，绝不碰行情表 |
| `mount("/")` 遮蔽 `/api/*` 与 `/health`（全站 404） | 高 | T7 强制"静态挂载在所有 router 之后"+ 专门测试 |
| `output: 'export'` 下 rewrites 失效，前端在 dev 正常、生产坏 | 高 | 一律相对路径 `/api/...` + `grep localhost` 自查（03 §5.1、T8 §4.2） |
| session 存进程内 → `--workers 2` 会随机丢登录态 | 中 | 与 baostock 单飞限制并列写进 README 部署章节，明确"单实例" |
| Cookie 鉴权引入 CSRF 面 | 中 | `SameSite=Strict` + 写操作要求 `X-Sequoia-Client`（03 §4） |
| 浏览器持有 API Key（本应避免） | 中 | 只用 API Key 换 HttpOnly session，不落任何前端持久化存储（DoD 16） |
