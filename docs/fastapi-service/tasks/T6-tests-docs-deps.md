# T6 · 测试收口、依赖锁定与文档运维

**依赖**：T1..T5 全部合入　**阻塞**：无（收口任务）
**规模**：1 个脚本 + 改 README/架构文档 + 补漏测试 + 依赖一致性核查

## 1. 目标

前面五个任务各自带了自己的测试，本任务负责**整体**：
跨层集成测试、依赖与锁文件一致性、面向使用者的文档改写、一条命令的冒烟验证脚本，
以及把执行过程中暴露的偏差回写进本目录文档（**文档必须与实际代码一致**，
否则下一个改动它的人会被误导）。

## 2. 动手前先读

- [README.md §4 Definition of Done](../README.md) —— 本任务的验收就是它
- `README.md` 仓库根 —— 现状文档，你要重写其中"运行方式"章节
- 约束全文（尤其 §6 外部副作用、§10 交付纪律）
- T1..T5 各自的"完成后汇报"，把其中提到的未尽事项变成你的任务清单

## 3. 文件清单

新建：`scripts/smoke_test.ps1` 与 `scripts/smoke_test.sh`（跨平台两份，内容等价）、
`tests/test_integration_service.py`
修改：`README.md`、`pyproject.toml`（仅当版本/依赖声明需收口）、`uv.lock`、
本目录下任一与实际代码不符之处

可选修改：`sequoia_x/**` —— **仅限修掉前面任务遗漏的明显缺陷**
（如未导出的符号、`__init__.py` docstring 缺失、ruff 报错）。
**不得新增功能、不得调整接口签名。** 发现接口设计问题 → 汇报，由开发经理决定回哪个任务返工。

禁止改动：`data/*.db`、`tests/test_main.py`、`.env`（用户的真实配置，**永远不碰**；
只改 `.env.example`）。

## 4. 实现要点

### 4.1 依赖一致性核查

- `pyproject.toml` 的 `[project].dependencies` 应含：
  原 7 项 + `fastapi`、`uvicorn[standard]`、`apscheduler>=3.10,<4`。
- `[project.optional-dependencies].dev` 应含：原 3 项 + `httpx`、`pytest-asyncio`（若 T3 用到）。
- `requires-python` 保持 `>=3.10`（新增依赖都兼容）。
- 执行 `uv sync --extra dev`，确认 `uv.lock` 有变更则一并提交。
- **验证干净环境可复现**：`uv sync --extra dev --frozen` 不报错（`--frozen` 证明锁文件
  与声明一致，这是"别人 clone 下来能不能跑"的唯一凭据）。
- 检查是否引入了**未被 import 的依赖**或**被 import 但未声明的依赖**：
  后者致命（本机装了就跑、别人机器上 `ModuleNotFoundError`）。
  用 `python -c "import sequoia_x.api.app"` 在全新 venv 语义下验证。

### 4.2 集成测试

`tests/test_integration_service.py` —— 端到端，全部 mock 掉网络：

- 用 `tmp_path` 造独立行情库与任务库（patch `Settings.db_path` / `task_db_path`），
  patch `pipeline` 里的 `DataEngine` 与 `FeishuNotifier`。
- 场景 1「API 全链路」：起 `TestClient` → `POST /api/tasks/daily{push:true}` →
  轮询 `GET /api/tasks/{id}` 到 `success` → `GET /api/tasks/{id}/signals` 有数据 →
  `GET /api/signals?date=` 能查到同一条 → `mock notifier.send` 被调用次数 == 有结果的策略数。
- 场景 2「单飞」：daily 运行中 `POST /api/tasks/daily` 与
  `POST /api/tasks/backfill` → 后者 409。
- 场景 3「CLI 与 API 等价」：同一份 mock 环境下分别调 `pipeline.run_daily(settings)`
  与经由 API 提交的同一任务，断言两者的 `outcomes` 结构一致、
  推送调用序列一致（策略顺序、`webhook_key`、symbols 全等）。
  **这条测试是"双形态共存"决策的守卫**，最有价值。
- 场景 4「重启恢复」：手工往 `tasks.db` 插一条 `status=running` 的僵尸记录 →
  新建 `TaskManager` 并 `start()` → 断言该记录变 `failed`、
  `active_task()` 返回 `None`、**随后 `submit()` 能成功**（不会永久卡死）。
- 场景 5「鉴权矩阵」：`/health` 免鉴权、`/api/*` 全部 401、带 key 全部非 401。
  用一个参数化测试遍历所有已注册路由（从 `app.routes` 自动生成用例），
  **这样新增路由忘了挂鉴权会被立刻抓出来**。

### 4.3 冒烟脚本

`scripts/smoke_test.sh`（POSIX，Git Bash 可跑）与 `scripts/smoke_test.ps1`，逻辑：

1. `pytest -q` → 失败即退出。
2. 后台起 `uvicorn sequoia_x.api.app:app --port <随机高位端口>`，等 `/health` 返回 200
   （**最多 30 秒**，超时报错并打印子进程输出）。
3. 无 key 访问 `/api/tasks` 期望 401；带 key 期望 200。
4. `GET /api/strategies` 期望 7 条。
5. **不触发任何跑批接口**（会真推飞书、真打 baostock，约束 §6）。
   脚本顶部注释明确写这句，防止后来人"顺手加个 POST /api/tasks/daily 试试"。
6. 杀进程、清理、打印通过摘要。任何失败路径都要留下服务日志尾部便于排查。

端口用随机值，避免与已运行实例冲突；脚本必须可重复执行。

### 4.4 README 改写

`README.md` 需要更新的位置与要点：

- 顶部简介：说明项目现在有两种运行形态（CLI / HTTP 服务）。
- 「两种运行模式」章节（现在 L17-22）：保留 CLI 说明，**新增**「作为服务运行」：
  安装 → 配置 → `uvicorn sequoia_x.api.app:app` → `curl /health` →
  触发跑批 → 查任务状态，一条可复制粘贴的命令链。
- 新增「API 一览」表格（照架构 §4 全表，注明鉴权与状态码语义）。
- 新增「定时调度」章节：`SCHEDULER_ENABLED` 默认关闭、如何开启、
  cron 表达式与时区、**以及"开启前确认飞书 webhook 配对了没有"**（否则到点误推）。
- 新增「生产部署注意」：
  - 默认 `127.0.0.1`；对外暴露**必须**配 `API_KEY` 并前置反代做 TLS。
  - 单实例部署：进程内单飞锁与 baostock 全局 socket 决定了
    **不能水平扩容成多副本**（多 worker 同理，`uvicorn --workers 2` 会有两个调度器
    各自到点跑批 → 双份推送 + baostock 互踩）。这条要**加粗**，是最容易踩的运维坑。
  -  systemd / supervisor 托管示例（`EnvironmentFile` 或 cwd 要求：`.env` 与 `data/` 都按
    相对路径解析，工作目录必须是仓库根 —— 这条来自 `config.py:13` 的 `env_file=".env"`
    与 `db_path` 默认值，属实际约束，验证后写进文档）。
- 「目录结构」章节（现在 L84-109）：更新为架构 §2 的新结构。
- crontab 建议段落：改为"内置调度"或"用外部 cron 调 `python main.py`（无需启动服务）"
  两条路径并列说明。
- 补「故障排查」小节，至少覆盖：
  401 → key 未配；409 → 已有任务在跑（给出查 `/api/tasks` 的命令）；
  `scheduler_running=false` 但 `scheduler_enabled=true` → cron 写错；
  baostock `10001011` → 已被限流，停止重试等待恢复；
  `/api/market` 409 `database_not_seeded` → 先跑回填。

### 4.5 文档回写与偏差修正

- 通读 `docs/fastapi-service/**` 与实际代码，把所有"文档说的 ≠ 代码做的"改掉。
  典型候选：接口路径变更、`StrategyOutcome` 字段名、`TaskRecord.to_dict()` 实际键名、
  T2 关于日志挂载方案的最终选择、T4 是否真的动了 `pipeline.py`。
- 若某个约束在执行中被放宽（例如 `max_workers` 不是 1），**改代码回到文档，不要改文档迁就代码**，
  除非有充分理由并在文档里记录理由。
- 在 `docs/fastapi-service/README.md` 风险登记册追加 T4 §4.4 承诺的交易日历 TODO 一行。
- 本目录文档的链接全部为相对链接，改完确认无死链（`grep -o '](./[^)]*'` 逐个点一下）。

## 5. 验收标准

`docs/fastapi-service/README.md` §4 的 **12 条 Definition of Done 逐条验证并给出证据**
（命令 + 输出片段），这是本任务唯一的验收标准。

额外：

```bash
uv sync --extra dev --frozen     # 锁文件一致性
ruff check .                     # 全仓干净
pytest -q                        # 全绿，且无 warning 淹没的真实错误
bash scripts/smoke_test.sh       # 冒烟通过（Windows 上跑 powershell -File scripts/smoke_test.ps1）
```

逐条自查：

1. 集成测试场景 3（CLI/API 等价）**存在且通过** —— 少了它等于没守住核心决策。
2. 场景 5 的鉴权遍历覆盖了 `app.routes` 里**所有** `/api/` 路由（打印用例数确认）。
3. README 里每条命令都**实际执行过**，不是照着代码想象出来的。
4. `uv sync --extra dev --frozen` 成功。
5. `git status` 里没有 `data/`、`.env`、`__pycache__`、`.idea/`（这些已在 `.gitignore`）。
6. `grep -rn "TODO\|FIXME\|XXX" sequoia_x/` 无新增（约束 §8 禁止占位）。
7. 冒烟脚本**没有**调用任何触发类接口（读脚本确认）。
8. 全部测试在无网络环境下通过（拔网线或临时禁出网验证一次）。

## 6. 完成后汇报

- 12 条 DoD 的逐条结论（通过/未通过 + 证据），**不许笼统说"全部通过"**
- 新增测试文件与用例数、全量 pytest 的输出尾部
- 修掉的 T1..T5 遗漏缺陷清单
- 文档回写：改了哪些行、原文与代码的实际差异是什么
- 明确未做/待议事项（如交易日历），供开发经理决定下一期
