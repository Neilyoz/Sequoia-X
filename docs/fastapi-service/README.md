# Sequoia-X 服务化改造（CLI → FastAPI）工作细则

> 本目录是本次改造的**唯一事实来源**。所有执行 agent 必须先完整阅读
> [01-constraints.md](./01-constraints.md) 与 [02-architecture.md](./02-architecture.md)，
> 再阅读自己被指派的任务文件，然后动手。文档与你的直觉冲突时，**以文档为准**；
> 文档与现有代码事实冲突时，**停下来汇报，不要自行改设计**。

## 1. 目标与已确认的产品决策

把项目从"只能 `python main.py` 手动跑批"改造成**常驻 FastAPI 服务**，同时保留 CLI。

以下四项已与需求方确认，不再讨论：

| 决策点 | 结论 |
| --- | --- |
| CLI 去留 | **双形态共存**。`python main.py` / `--backfill` 行为逐字节不变，CLI 与 API 共用同一编排层 |
| 定时调度 | **内置 APScheduler + API 手动触发**，可通过 `.env` 整体开关 |
| 长任务处理 | **异步后台任务 + 状态查询**。提交即返回 `task_id`，全局单飞互斥 |
| 交付范围 | **服务化核心 + 运维配套**：触发/状态/选股结果/行情查询接口 + API Key 鉴权 + 测试 + 文档 |

明确**不做**（超出本次范围，需求方选了"服务化核心"而非"完整平台化"）：

- 策略参数的在线增删改、策略热加载、 Web 前端页面
- 多 worker / 分布式 / 队列中间件（Redis、Celery）
- 把 `strategy.run()` 改造成 `async def`
- 用户体系、OAuth、审计日志

## 2. 任务拆分与依赖顺序

```
T1 runner 编排层 ──┬── T2 任务管理 ────┬── T3 FastAPI 应用 ──┬── T4 定时调度
                   │                   │                     │
                   └───────────────────┴── T5 查询接口 ◄─────┘
                                                │
                   T1..T5 全部完成 ─────────────┴── T6 测试/依赖/文档收口
```

**必须串行执行**：T1 → T2 → T3 → T4 → T5 → T6。

原因见 [01-constraints.md §0](./01-constraints.md)。简言之：这些任务改的是同一条调用链，
且 T3 的路由要 import T2 的 `TaskManager`。**禁止并行派单**，除非两个任务的文件集合完全不相交
（例如 T4 与 T5 在 T3 合入后可并行，但两者都要独占自己的测试文件）。

| 任务 | 文件 | 产出 | 依赖 |
| --- | --- | --- | --- |
| T1 | [tasks/T1-runner-layer.md](./tasks/T1-runner-layer.md) | `sequoia_x/runner/`、`core/bootstrap.py`、瘦身后的 `main.py` | — |
| T2 | [tasks/T2-task-system.md](./tasks/T2-task-system.md) | `sequoia_x/task/`（模型/存储/管理器） | T1 |
| T3 | [tasks/T3-fastapi-app.md](./tasks/T3-fastapi-app.md) | `sequoia_x/api/` 应用与触发路由 | T2 |
| T4 | [tasks/T4-scheduler.md](./tasks/T4-scheduler.md) | `sequoia_x/scheduler/` 与 lifespan 接线 | T3 |
| T5 | [tasks/T5-query-apis.md](./tasks/T5-query-apis.md) | 选股结果 / 行情 / 策略清单查询路由 | T3 |
| T6 | [tasks/T6-tests-docs-deps.md](./tasks/T6-tests-docs-deps.md) | 测试补全、依赖锁定、README 与部署文档 | T1..T5 |

## 3. 派单方式（给开发经理用）

给每个 agent 的提示词模板，按 `{{}}` 填充后直接下发：

```
你是 Sequoia-X 项目的实现工程师。项目根目录 D:\quant_stock\Sequoia-X。

任务：完成 docs/fastapi-service/tasks/{{T3-fastapi-app.md}} 中定义的全部内容。

必读（按顺序，全文读完再动手）：
1. docs/fastapi-service/01-constraints.md   ← 全局硬约束，违反即返工
2. docs/fastapi-service/02-architecture.md  ← 目标架构与目录规划
3. docs/fastapi-service/tasks/{{T3-fastapi-app.md}}  ← 本次任务
4. 已完成任务 {{T1、T2}} 的实际产出代码（以代码为准，不要只看文档）

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
4. 配置 `API_KEY` 后，无 `X-API-Key` 头的 `/api/*` 请求返回 401；`/health` 仍免鉴权。
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
