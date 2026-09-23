# 01 · 全局硬约束（所有任务必读，违反即返工）

本文列出从现有代码里读出来的**不可协商事实**。它们不是风格偏好，破坏任何一条会导致运行时故障、
测试变红或用户数据丢失。每条都给了代码出处，动手前请自己点进去确认一遍。

---

## §0 为什么必须串行

`main.py` → `runner` → `task manager` → `api routes` 是一条单向调用链，下游 import 上游。
两个 agent 并行改这条链会在 import 层面互相引用不存在的符号。此外本仓库是**单个 git 工作树**，
没有为并行改动准备隔离分支。**默认串行派单。**

---

## §1 启动期副作用：顺序即正确性

现有 `main.py:8-20` 严格按此顺序：

```python
import argparse, sys
from dotenv import load_dotenv
load_dotenv()                    # ① 必须在任何 Settings 实例化之前
from datetime import date
import socket
socket.setdefaulttimeout(60.0)   # ② 注释明确写了为什么是 60 不能调小
from sequoia_x.core.config import get_settings   # ③ 之后才 import 业务模块
```

约束：

1. **`load_dotenv()` 必须先于任何 `get_settings()` 调用。** `Settings` 是
   `sequoia_x/core/config.py:74-92` 的进程级单例，一旦实例化就不会重载 `.env`。
   注意 `config.py` 本身 import 时并不实例化，所以"import 它"是安全的，
   但**任何模块顶层不得调用 `get_settings()`**。
2. **`socket.setdefaulttimeout(60.0)` 不得下调。** 原文注释：baostock 走裸 socket 并继承该
   "单次 recv"超时，其全市场证券列表单次耗时 60~72 秒、分块间隔实测可达 4 秒。
   设成 10s 会把它掐死成"接口超时"。新增的 akshare 依赖（`PrivatePlacementStrategy`）
   不接受 `timeout` 参数，同样靠这个全局兜底。
3. 以上两条统一收口到 **`sequoia_x/core/bootstrap.py:bootstrap()`**，CLI 入口与 API 入口
   **都必须最先调用它**，且不得在 `bootstrap()` 之前 import 任何 `sequoia_x.*` 业务子模块。
   `bootstrap()` 必须是幂等的（重复调用无副作用）。

理由：uvicorn 导入 app 的路径和 CLI 完全不同，不收口就会出现"CLI 能跑、API 跑不了"的分裂故障。

---

## §2 CLI 兼容性契约

1. `python main.py` 与 `python main.py --backfill` 的**命令行签名不变**，
   行为与改造前一致（包括日志文案与执行顺序）。
2. **未捕获异常 → `sys.exit(1)`**。`tests/test_main.py:14-23` 用 hypothesis 验证：
   patch 掉 `main` 模块里名为 `get_settings` 的模块级属性后，`main.main()` 必须抛
   `SystemExit` 且 `code != 0`。因此：
   - `main.py` 里必须保留 `from sequoia_x.core.config import get_settings` 这一**模块级名字**，
     并且在 `try:` 块**内部**调用它；
   - 不得把 `main()` 改成 `async`；
   - 不得用 `from sequoia_x.core import config` 然后 `config.get_settings()` 代替（patch 会失效）。
3. `main()` 保持同步函数语义，编排逻辑下沉到 `sequoia_x/runner/pipeline.py`。
   **runner 层禁止调用 `sys.exit()`、禁止 `print()`**，只允许记日志和抛异常 ——
   退出码是 CLI 的职责，不是库的职责。

---

## §3 并发：baostock 是进程级全局单连接

这是整个改造最硬的约束。

- `sequoia_x/data/baostock_guard.py:29-49` 直接读写 `baostock.common.context.default_socket`
  这个**模块全局变量**。`drop_connection()` 关的就是它。
- 结论：**同一进程内任意时刻只能有一个使用 baostock 的任务在跑**。两个任务并发时，
  A 的 `drop_connection()` 会掐断 B 正在用的 socket，表现为 B 随机"接口超时"，
  连续失败后触发服务端限流 `10001011`（该限流会对本机 IP 持续一段时间，
  届时**连验证都不能做，只能用极小样本**）。
- `akshare` 同理不宜并发轰炸东方财富接口。

强制要求：

1. `TaskManager` 使用 **`ThreadPoolExecutor(max_workers=1)`**，并且额外用一把
   `threading.Lock` 保护的 `_current_task_id` 做**显式单飞判定**。
2. 已有任务处于 `pending`/`running` 时，新提交**直接拒绝并返回 409**，
   **不要排队**。排队会让用户重复点击堆积出一串注定失败的跑批。
3. CLI 与调度器共用同一个 `TaskManager`（服务态）或同一个 pipeline（进程态），
   不得出现"绕过单飞"的第二条提交路径。
4. 任何新增路由若会触达 `DataEngine` 的 baostock/akshare 方法
   （`sync_today_bulk` / `backfill` / `sync_stock_basic`），必须走 `TaskManager`。
   只读查询（`get_ohlcv` / `get_stock_names` / `get_local_symbols`）可以直连，见 §5。

---

## §4 multiprocessing.Pool 在工作线程内

`DataEngine.sync_today_bulk()`（`sequoia_x/data/engine.py:175-234`）在方法内部
`from multiprocessing import Pool` 并 `with Pool(n_workers) as pool: pool.map(...)`，
最多 8 进程。服务化后这条路径会被跑在 `ThreadPoolExecutor` 的工作线程里。

已知情况：

- `multiprocessing.Pool` 会检查 `current_process().daemon`；普通线程里
  `current_process()` 返回 `MainProcess`（`daemon=False`），所以**不会**触发
  "daemonic processes are not allowed to have children"。这条理论上是安全的。
- 但本机是 **Windows + Python 3.14**，子进程启动方式为 `spawn`：子进程会重新解释并 import
  目标函数的所属模块。`_bs_fetch_batch` 是 `engine.py` 的**模块级函数**，可 pickle，
  但 spawn 出来的子进程**不会执行 `main.py` 的顶层代码**，因此 §1 的两个副作用
  （`load_dotenv` / `setdefaulttimeout`）在子进程里**不成立**。当前代码里子进程只用
  baostock 且不读 `.env`，所以历史上没暴露；改造后不得引入子进程读配置的新逻辑。

强制要求（T2 任务的验收项）：

1. 必须在服务态**实测一次** `sync_today_bulk()`，把结果写进交付报告。
2. 若实测出现 `Pool` 相关失败（Windows spawn、端口/信号、内存），**降级方案**：
   给 pipeline 增加 `use_multiprocessing: bool = True` 开关，服务态提交的任务传 `False`，
   走单线程同步路径。CLI 保持 `True` 不变。
3. 严禁为了让 Pool 好用而修改 `engine.py` 的同步算法本身。开关只是包裹现有分支。

---

## §5 SQLite 读写规则

- 行情库：`Settings.db_path`，默认 `data/sequoia_v2.db`，**当前约 456 MB，是用户的真实数据**。
  本次改造**禁止迁移、禁止重命名、禁止清空、禁止改 schema 中已有的
  `stock_daily` / `stock_basic` 两张表**。`_init_db()` 只有 `CREATE TABLE IF NOT EXISTS`，
  保持现状。
- `DataEngine` 每次操作独立 `sqlite3.connect()`，无连接池 —— 对"读多写少 + 单写者"友好。
  Web 并发读可以接受；写仍然只发生在跑批路径，已被 §3 单飞串行化。
- **任务历史与选股结果放独立库** `data/tasks.db`（`Settings.task_db_path`）。
  理由：`sync_today_bulk` 里有 `DELETE FROM stock_daily WHERE date = ?` 这类破坏性写入，
  同库混放会让"误删任务表"和"任务表膨胀影响行情库"两类风险互相污染。
- 新增表一律 `CREATE TABLE IF NOT EXISTS`，且**只允许出现在 `tasks.db`**。
- 查询接口必须走只读连接；能带上 `LIMIT` 的一律带 `LIMIT`（默认 100，硬上限 1000），
  防止 `GET /api/market/.../ohlcv` 把 456 MB 库里几十万行拉进内存再序列化。

---

## §6 飞书推送的外部副作用

`FeishuNotifier.send()`（`sequoia_x/notify/feishu.py:88-131`）会向真实群 webhook 发消息，
**这是不可撤回的外部动作**。

- 生产语义下 API 手动触发应当能**只跑不推**，因此 `POST /api/tasks/daily` 支持
  `push: bool`，**默认 `true`**（与 CLI 行为保持一致，避免"接口跑出来和命令行不一样"的困惑）。
  调试用 `push=false`。
- **`SCHEDULER_ENABLED` 默认 `false`。** 否则任何人 clone 后 `uvicorn` 一开，
  到点就往真人群里推消息。
- 改造**不得修改** `FeishuNotifier` 的卡片样式、名称查询逻辑
  （`f866c75` 专门恢复了从 baostock/本地 `stock_basic` 取名称）与
  "HTTP 失败只记 ERROR 不抛异常"的容错语义。
- 测试**必须 patch 掉网络发送**，绝不允许在 CI/单测里真发消息。

---

## §7 配置扩展的向后兼容

`Settings`（`sequoia_x/core/config.py`）现在只有 4 个字段，`extra="ignore"`，
唯一必填项是 `feishu_webhook_url`。用户手里已有 `.env` 只有老键。

- 新增字段的**默认值必须让"老 `.env` 不改一个字也能启动"**。
- 除 `feishu_webhook_url` 外不得新增必填字段。`API_KEY` 允许为空，语义为"不启用鉴权"，
  并且此时必须把 `API_HOST` 默认锁在 `127.0.0.1`（见 T3）。
- `config.py` 里 `settings_customise_sources` 与 `model_post_init` 的
  `STRATEGY_WEBHOOK_` 前缀扫描逻辑（L19-57）是刻意写复杂的，**不要顺手重构**。
  新增字段用普通声明即可。
- 所有新配置项同步进 `.env.example`，中文注释，含示例值与"默认值/风险说明"。

---

## §8 编码规范

- Python ≥ 3.10 语法（本机 venv 为 3.14），ruff `line-length = 100`，
  `select = ["E", "F", "I", "UP"]`。**导入必须 isort 有序**，否则 ruff 报 I001。
- 全量类型标注；公开函数必须有中文 docstring（现有文件都是这个风格，保持一致）。
- **注释、日志、文档、commit message 一律中文**。代码标识符用英文。
- 注释只写"为什么"，不写"是什么"。现有代码里的注释都在解释坑（见 `baostock_guard.py`
  模块级 docstring），新注释请保持同等密度而不是灌水。
- 禁止：新增 ORM/迁移工具（alembic）、新增日志库、引入 `async def` 改造策略层、
  提前 `TODO` 占位、留注释掉的死代码。
- 每个新目录要有带中文 docstring 的 `__init__.py`，风格对齐
  `sequoia_x/strategy/__init__.py`。

---

## §9 测试要求

- 测试目录固定 `tests/`（`pyproject.toml` 的 `testpaths` 已配）。
- 沿用现有风格：普通 `pytest` + `unittest.mock.patch` + `hypothesis` 属性测试。
  新增异步测试允许引入 `pytest-asyncio`，但**优先用 `TestClient` 走同步 HTTP**，
  它已能覆盖 FastAPI 的 async 路由。
- 命名：`test_<被测行为>_<场景>`，docstring 用中文说明它守的是哪条性质。
  现有属性测试有编号约定（如"Property 13"），新增性质请续号，
  并在 docstring 里与本文档 §编号互相指认。
- 每个任务的验收标准里都写了"必须新增哪些测试"。**没有测试的实现视为未完成。**
- 全量测试必须能在**离线**环境通过（不访问 baostock/akshare/飞书）。
  跑批类测试一律 mock `DataEngine` 与 `FeishuNotifier`。

---

## §10 交付纪律

1. 一个任务一次提交，commit message 中文，遵循仓库现有前缀风格：
   `feat(api): ...` / `refactor(runner): ...` / `test(task): ...` / `docs: ...`。
2. **不得 `git push`、不得建 PR、不得 `--no-verify`。** 推送由需求方决定。
3. 提交前跑：`ruff check .`、`pytest -q`。有配置过滤器导致工作树状态不明时，
   先 `git status` 看清再 `git add`，**逐文件 add，禁止 `git add -A`**。
4. 遇到文档没覆盖的决策点：**停下来问**，不要发明。
5. 报告改动时要如实说明"文档说的"与"代码实际"不一致之处 —— 前面调研已发现
   文档型描述可能滞后于代码，你的复核对后续任务有价值。
