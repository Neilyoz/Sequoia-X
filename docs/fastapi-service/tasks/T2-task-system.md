# T2 · 异步任务管理系统

**依赖**：T1　**阻塞**：T3、T4、T5
**规模**：4 个新文件 + 改 1 个配置文件 + 3 个测试文件

## 1. 目标

提供 `TaskManager`：把 `runner.pipeline` 的同步长任务丢进后台线程执行，
提交立即返回 `task_id`，全局单飞互斥，任务状态与选股结果持久化到独立的 `data/tasks.db`，
并把每个任务的日志按 `task_id` 归集起来供后续查询。

**本任务不写任何 HTTP 代码。** HTTP 路由是 T3 的事。

## 2. 动手前先读

- 约束 [§3 并发](../01-constraints.md)、[§4 Pool](../01-constraints.md)、
  [§5 SQLite](../01-constraints.md) —— 全文最重要的一节
- 架构 [§3.4 §3.5 §3.6 §3.7](../02-architecture.md) —— 接口签名照抄，不要自创
- T1 的实际产出：`sequoia_x/runner/pipeline.py`、`sequoia_x/core/bootstrap.py`
- `sequoia_x/data/engine.py:175-234` —— `sync_today_bulk()` 里的 `multiprocessing.Pool`

## 3. 文件清单

新建：`sequoia_x/task/__init__.py`（中文 docstring：`"""任务层：长任务的后台执行、单飞互斥、状态与结果持久化。"""`）、
`models.py`、`store.py`、`logs.py`、`manager.py`
修改：`sequoia_x/core/config.py`（只加字段，见 §4.1）
测试：`tests/test_task_store.py`、`tests/test_task_manager.py`、`tests/test_task_logs.py`

禁止改动：`main.py`、`sequoia_x/runner/**`（除 §4.5 明确允许的接线）、
`sequoia_x/data/engine.py`、`sequoia_x/strategy/**`、`sequoia_x/notify/**`、
`pyproject.toml`（本任务只用标准库 `threading`/`sqlite3`/`logging`/`contextvars`，无需新依赖；
`fastapi`/`uvicorn`/`httpx` 由 T3 引入，`apscheduler` 由 T4 引入，T6 只做最终一致性收口）。

> **例外**：若 §5 的 Pool 实测结论要求给 `sync_today_bulk` 加 `use_multiprocessing` 开关，
> 那属于**必须回来改 `engine.py`** 的情况。此时：改动仅限新增一个默认 `True` 的关键词参数
> 和一个 `if` 分支（分支内走现有的串行替代路径），改完必须重新跑通 `tests/test_data_engine.py`，
> 并在汇报里单独列出这段 diff 请求复核。

## 4. 实现要点

### 4.1 `config.py` 新增字段

在 `Settings` 里追加（全部有默认值，保持约束 §7 的向后兼容）：

```python
task_db_path: str = "data/tasks.db"
task_history_limit: int = 200
task_log_tail_lines: int = 200
```

字段声明放在 `start_date` 之后、`feishu_webhook_url` 之前或之后均可，
但**不要碰** `settings_customise_sources` / `model_post_init` / `get_webhook_url`（约束 §7）。
`.env.example` 本任务同步补这三行 + 中文注释。

### 4.2 `models.py`

按架构 §3.4 定义。要点：

- `TaskKind` / `TaskStatus` 用 `str, Enum` 双继承，保证 JSON 序列化直接得到字符串。
- `TaskRecord.to_dict()` 输出对外契约形态（枚举展开为 `.value`，时间保持 ISO 字符串）。
- 时间戳统一走一个内部私有函数 `_now_iso()`：
  `datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")`。
  `models.py` 里 timezone 需要注入 —— 简化处理：模块级常量 `TIMEZONE` 从
  `os.environ.get("TIMEZONE", "Asia/Shanghai")` 读，并在注释里说明
  "与 Settings.timezone 同源，避免 models 反向依赖配置单例"。
  若 `ZoneInfo` 在本机不可用（Windows 无 tzdata 时可能抛），**回退到固定 UTC+8**：
  用 `timezone(timedelta(hours=8))`。这个回退必须有注释解释原因。

### 4.3 `store.py`

- 表结构 DDL 逐字照抄架构 §3.5（含索引名）。
- **每次操作独立 `sqlite3.connect()` + `with` 语句**，与 `DataEngine` 风格一致，
  不要引入连接池或线程本地连接（任务写入频率极低，没必要）。
- `update()` 用 `INSERT INTO task_run ... ON CONFLICT(task_id) DO UPDATE SET ...`
  （SQLite 3.24+，Python 3.14 自带版本远高于此），或者老实地 `UPDATE` 全字段 ——
  二选一，**不要两种都实现**。
- `active()`：`SELECT ... WHERE status IN ('pending','running') ORDER BY created_at LIMIT 1`。
  这是单飞判定的**兜底**（进程重启后内存标志丢失时靠它）。
- `save_signals()`：把 `DailyReport.outcomes` 摊平成 signal 行，`INSERT OR IGNORE`
  （靠 `UNIQUE(task_id, strategy, symbol)` 幂等）。返回写入行数。
- `prune(keep)`：删除 `task_run` 中除最近 `keep` 条外的记录，以及 `signal` 中
  `task_id` 不在保留集合里的行。两条 SQL 足够，**不要顺手清行情表**（约束 §5）。
- 提供 `recover_interrupted()`：把所有非终态记录改成 `failed` +
  `error="进程重启导致任务中断"`，返回受影响条数。启动时调用。

### 4.4 `logs.py`

```python
class TaskLogCapture(logging.Handler):
    """按 task_id 归集 sequoia_x.* 日志行的 Handler。"""
```

- `current_task_id: ContextVar[str | None]`，模块级导出
  `set_current_task(task_id | None)` 与 `get_current_task()`。
- `emit()` 读 `get_current_task()`，无值直接 `return`；有值则把
  `f"{record.created_at 格式} {record.levelname} {record.getMessage()}"`
  追加进 `deque(maxlen=tail_lines)`（**每个 task_id 一个独立 deque**，
  存在 `self._buffers: dict[str, deque]`，`get_lines(task_id)` 取出）。
- 挂载点：`logging.getLogger("sequoia_x")` 上 `addHandler`（它的子 logger 会 propagate 上来 ——
  注意 `core/logger.py:37` 对**具体模块 logger** 设了 `propagate = False`，
  所以挂在 `"sequoia_x"` 父 logger 上**收不到子 logger 的日志**！）。
  → **正确做法：`TaskManager` 在启动时把 capture handler 挂到每个已存在的
  `sequoia_x.` 前缀 logger 上**（遍历 `logging.Logger.manager.loggerDict`），
  并且给 `get_logger()` 加一个可选的"额外 handler 注册"钩子。
  **先读 `core/logger.py:25-38` 验证这个结论再动手**；若你验证下来挂父 logger 就能收到，
  用简单方案并在汇报里说明你观察到的实际 propagate 行为。
- 不得给 handler 设置 rich 格式化，纯文本即可（要落库）。
- 单测必须覆盖：并发两个"任务上下文"下各自只收到自己的行。

### 4.5 `manager.py`

- `submit()` 的临界区必须在一把 `threading.Lock` 内完成
  "读 active → 判定 → 建 PENDING 记录 → 置内存标志 → `executor.submit`"。
  **锁范围要覆盖到把任务真正交进线程池**，否则两个并发请求能同时通过 active 检查。
- 执行体伪码：

```python
def _execute(self, task_id, kind, params):
    set_current_task(task_id)
    record = self._store.get(task_id)
    record.status = RUNNING; record.started_at = _now_iso()
    self._store.update(record)
    try:
        if kind is TaskKind.DAILY:
            report = pipeline.run_daily(self._settings,
                                        push=params.get("push", True),
                                        strategies=params.get("strategies"))
            record.result = report.to_dict()
            self._store.save_signals(task_id, report.trade_date, report.outcomes)
        else:
            record.result = pipeline.run_backfill(self._settings)
        record.status = SUCCESS
    except BaseException as exc:              # 抓 BaseException：线程里 Exception 之外的
        record.status = FAILED                # 也要推进终态，否则单飞永久卡死
        record.error = f"{type(exc).__name__}: {exc}"
        logger.exception(...)
    finally:
        record.finished_at = _now_iso()
        record.result = {**(record.result or {}),
                         "log_tail": self._capture.drain(task_id)}
        self._store.update(record)
        set_current_task(None)
        with self._lock:
            self._current_task_id = None
```

- `_current_task_id: str | None` 是内存态单飞标志；`submit()` 判定时
  **先查内存，内存为空再查 `store.active()`**（后者兜底重启）。
- `shutdown(wait=False)`：`executor.shutdown(wait=wait, cancel_futures=False)`。
  **正在跑的任务不强杀**（baostock 连接被硬切更糟），只拒绝新提交。
- 提供 `active_task()` 给路由层组装 409 详情。
- `wait_for_completion(task_id, timeout)`：轮询 `store.get()` 到终态，
  `time.sleep(0.05)` 步进，超时抛 `TimeoutError`。**明确标注只供测试与调度器使用**。
- **不要引入 `asyncio`**。整个 `task/` 包是纯同步的，T3 在路由里用
  `run_in_threadpool` 或直接调用（这些操作都是微秒级 SQLite 读）。

## 5. 必做实验：`multiprocessing.Pool` 在服务态可用性

这是约束 §4 要求的实测，**必须真做并把结果写进汇报**。

方法（临时脚本，跑完删除，**不要提交进仓库**）：

1. 用 `tmp_path` 造一个几行的 `tasks.db` 与一个最小行情库（建表 + 插 3 只股票各 2 天数据，
   最后一个日期设为昨天，让 `sync_today_bulk` 认为需要更新）。
2. 在 `ThreadPoolExecutor(max_workers=1)` 的工作线程里调用 `engine.sync_today_bulk()`。
3. **patch 掉 baostock 网络层**（`_bs_fetch_batch` 或其内部 bs 调用），
   只验证"在工作线程里能否创建并使用 Pool"，不验证数据拉取。
   绝不为了这个实验去真连 baostock 打全市场请求 —— 会触发限流（约束 §3）。
4. 记录：能否正常返回、有无 `daemonic processes` / spawn / 权限报错、耗时。

结论分支：

- **可用** → 不改 `engine.py`，`run_daily` 的 `use_multiprocessing` 参数保持透传不用。
- **不可用** → 按 §3 的例外条款给 `sync_today_bulk` 加开关，
  并在 `manager._execute` 里对 `DAILY` 传 `use_multiprocessing=False`。
  CLI 路径保持 `True`。改完必须验证 CLI 仍正常（`tests/test_data_engine.py` + `tests/test_main.py` 全绿）。

## 6. 测试要求

`tests/test_task_store.py`
- 建表幂等：连续 `init_schema()` 两次不报错。
- create → update → get 往返，字段无损（含 `result_json` 嵌套 dict）。
- `active()` 在有 PENDING 时返回它，全终态后返回 `None`。
- `recover_interrupted()` 把 running 改 failed 并保留 `finished_at` 为空/填值的一致策略
  （你定，注释说明）。
- `save_signals()` 幂等（同 `(task_id, strategy, symbol)` 重复写不报错、不重复）。
- `prune(keep=2)`：造 5 条任务，保留最近 2 条，信号级联清理。
- `query_signals()` 按 `trade_date` / `start`+`end` 区间（闭区间）/ `strategy` / `symbol`
  过滤 + 分页正确。签名以架构 §3.5 为准（`start`/`end` 是给 T5 前端日期区间用的，
  本任务先把存储层能力建全，避免 T5 回头改 store）。

`tests/test_task_manager.py`
- patch `pipeline.run_daily` 为可控假函数（用 `threading.Event` 卡住它）。
- 提交立即返回（断言 `submit()` 耗时 < 0.5s 而假任务耗时 > 1s）。
- **单飞**：假任务运行中再 `submit()` → 抛 `TaskAlreadyRunning`，
  且异常携带的 `running_task_id` 正确。**这是本任务最重要的测试。**
- 并发压力：10 个线程同时 `submit()`，恰好 1 个成功、9 个抛异常
  （守约束 §3 的锁临界区）。
- 成功路径：终态 SUCCESS、`result` 含 `outcomes`、信号已落库。
- 失败路径：假 `run_daily` 抛 `RuntimeError` → 终态 FAILED、`error` 非空、
  **后续 `submit()` 能成功**（锁已释放）。
- 任务结束后 `_current_task_id` 归 `None`。

`tests/test_task_logs.py`
- 两个不同 `current_task_id` 上下文各写日志，缓冲区互不串台。
- 无 task_id 上下文时写日志不报错、不产生缓冲。
- `tail_lines` 上限生效（deque 截断）。

## 7. 验收标准

```bash
ruff check sequoia_x
pytest -q                        # 全绿；tests/test_main.py、test_data_engine.py 不得被改坏
python -c "from sequoia_x.task.manager import TaskManager"
python -c "from sequoia_x.core.config import get_settings; s=get_settings(); print(s.task_db_path)"
```

逐条自查：

1. `data/tasks.db` 能被自动创建，且 `data/sequoia_v2.db` 的 mtime **未变化**
   （`ls -l data/` 确认，约束 §5）。
2. 一个只含 `FEISHU_WEBHOOK_URL` 的老 `.env` 仍能加载 `Settings`（临时改名验证，别真改用户 `.env`）。
3. `grep -n "asyncio\|async def" sequoia_x/task/` 无结果。
4. 10 线程并发 `submit` 的测试确实存在且通过。
5. §5 的 Pool 实验结论已写进汇报。

## 8. 完成后汇报

- 文件清单、Pool 实验结论与（若有）`engine.py` 的额外 diff
- `logs.py` 挂载方案最终选了哪种、为什么
- 时间戳时区在 Windows 上的实测结论（`ZoneInfo("Asia/Shanghai")` 是否可用）
- 未尽事项
