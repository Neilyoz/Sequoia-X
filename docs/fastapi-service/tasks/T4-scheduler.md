# T4 · APScheduler 定时调度

**依赖**：T3　**可与 T5 并行**（文件集合不相交，但两者都会碰 `app.py` 的 router 注册，
并行时**由 T4 先合入 `include_router(scheduler 无关)` 的部分，T5 只加 queries router**）
**规模**：1 个新目录 2 个文件 + 改 `app.py` + 1 个测试文件

## 1. 目标

让服务在交易日自动跑批，替代现有 README 推荐的外部 crontab。
调度触发**必须复用 `TaskManager`**，从而与手动 API 触发共享同一把单飞锁。

## 2. 动手前先读

- 约束 [§3 并发](../01-constraints.md)、[§6 飞书副作用](../01-constraints.md)
- 架构 §3.7（`TaskManager.submit` / `TaskAlreadyRunning`）、§5 配置表
- `docs/fastapi-service/tasks/T3-fastapi-app.md §4.2` 的 lifespan（你要在里面接线）
- `README.md:76-80` 现有 crontab 建议（`15 19 * * 1-5`，这是默认值的出处）

## 3. 文件清单

新建：`sequoia_x/scheduler/__init__.py`（docstring：`"""调度层：基于 APScheduler 的交易日定时跑批。"""`）、
`sequoia_x/scheduler/jobs.py`
修改：`sequoia_x/api/app.py`（lifespan 里创建/启动/关闭 scheduler）、`pyproject.toml` + `uv.lock`
测试：`tests/test_scheduler.py`

禁止改动：`sequoia_x/task/**`、`sequoia_x/runner/**`、`sequoia_x/api/routes/**`、`main.py`、
`sequoia_x/data/**`（**尤其不要**给 `sync_today_bulk` 加 `force` 参数绕过 `last_date >= today`
跳过逻辑：非交易日调度会真的白跑，见 §4.4）。

## 4. 实现要点

### 4.1 选型与形态

- **APScheduler 3.x，`AsyncIOScheduler`**（跑在 uvicorn 的 event loop 里，
  比 `BlockingScheduler` 合适 —— 后者会占住主线程，跟 uvicorn 冲突）。
  依赖锁定 `apscheduler>=3.10,<4`（4.x API 与 3.x 差异大，本次按 3.x 写）。
  `pyproject.toml` 里加 `apscheduler>=3.10,<4.0`。
- 用 `CronTrigger.from_crontab(settings.schedule_cron)` 解析 5 段 cron。
  配置串非法时（`ValueError`）：**记 ERROR 日志 + 跳过启动调度器 + 服务照常起来**，
  绝不允许因为一个 cron 写错导致整个服务无法启动（那会连带所有查询接口一起挂）。
  启动结果要能在 `GET /api/info` 里看出来（见 §4.5）。

### 4.2 模块结构

```python
def build_scheduler(settings: Settings, manager: TaskManager) -> AsyncIOScheduler | None:
    """按配置装配调度器；未启用或 cron 非法时返回 None。"""

def register_daily_job(scheduler, settings, manager) -> None

def _daily_job(settings, manager) -> None:
    """调度回调：提交 DAILY 任务。"""
```

- `_daily_job` 内部：
  ```python
  try:
      record = manager.submit(TaskKind.DAILY, params={"push": True}, triggered_by="scheduler")
      logger.info(f"定时跑批已提交：{record.task_id}")
  except TaskAlreadyRunning as exc:
      logger.warning(f"定时跑批被跳过，已有任务在跑：{exc.running_task_id}")
  except Exception:
      logger.exception("定时跑批提交失败")
  ```
  **提交失败不得让调度器线程崩掉**（APScheduler 会记录但别依赖它）。
- `_daily_job` **不要**直接调 `pipeline.run_daily`。那样就绕过了单飞锁（约束 §3.3）。
- job 参数：`id="daily_pipeline"`、`replace_existing=True`、
  `misfire_grace_time=300`（服务重启错过 5 分钟内仍补跑）、
  `coalesce=True`（错过多次只补一次，堆积补跑等于自己打爆 baostock）。
- `max_instances=1` 显式设置：APScheduler 层的第二道单飞保险。

### 4.3 时区

`AsyncIOScheduler(timezone=settings.timezone)`。
`ZoneInfo("Asia/Shanghai")` 在 Windows 上可能因缺 tzdata 失败 —— T2 已经踩过这个点，
**沿用 T2 的时区解析辅助函数**（如果 T2 抽了工具就用它，没抽就在 `jobs.py` 里
`try: ZoneInfo(...) except: timezone(timedelta(hours=8))`，注释说明）。
调度器时区与 `task_run.created_at` 时区**必须一致**，否则日志里会出现"19:15 的任务
时间戳写着 11:15"这类对账灾难。

### 4.4 非交易日行为（明确不做，但要写清）

baostock 在非交易日没有新数据，`sync_today_bulk()` 内部会因
`last_date >= today_str` 全部跳过并返回 0（`engine.py:192-202`），随后 7 个策略
在无新数据的旧数据上跑，**可能选出与昨天相同的结果并再次推送飞书** —— 这是骚扰。

默认 cron `15 19 * * 1-5` 已排除周末，但**排除不了法定节假日**。本次处理：
- 在 `_daily_job` 提交前加一道廉价保护：查 `DataEngine` 的最新行情日期，
  若今天相对该日期没有新增（即 `sync` 之前看不出交易日信号）仍然会跑，
  **所以真正的保护点放在 pipeline 之后不合适**。
- 决定：**本任务只加"空结果不推送"的确认**，即检查 T1 的
  `run_daily` 在 `synced_rows == 0` 时是否仍会推送。若仍会，**在 `pipeline` 里加**
  `synced_rows == 0 and not force` → 记 WARNING `"今日无新增行情（可能非交易日），跳过策略执行"
  并返回空 `DailyReport`。这是 T4 唯一允许改 `runner/pipeline.py` 的点，
  改动必须最小（一个提前 return + 一条日志），且要新增对应单测：
  `synced_rows=0 → outcomes 为空、send 零调用`。
- 更完整的交易日历（akshare 交易日历 / baostock 交易日接口）**不在本次范围**，
  在 `docs/fastapi-service/README.md` 的风险登记册里由 T6 追加一行 TODO 说明。

### 4.5 lifespan 接线与可观测性

- `app.py` lifespan 进入段：`scheduler = build_scheduler(settings, manager)`；
  非 `None` 则 `scheduler.start()`；`app.state.scheduler = scheduler`。
- 退出段：`scheduler.shutdown(wait=False)`，`try/except` 包住（进程退出期异常不该刷屏）。
- `GET /api/info` 增加/修正字段：`scheduler_enabled`（配置值）与
  `scheduler_running`（`app.state.scheduler is not None`，即"实际起来了没有"）。
  两者不一致就是 cron 写错了 —— 这是唯一的排查线索，**必须都返回**。
  （改 `routes/system.py` 属 T3 文件，此处允许**只增字段**。）
- 关闭态日志：`logger.info(f"定时调度已启动：{settings.schedule_cron} ({settings.timezone})")`
  或 `logger.info("定时调度未启用（SCHEDULER_ENABLED=false）")`。

## 5. 测试要求

`tests/test_scheduler.py`（不依赖真实时间流逝）

- `scheduler_enabled=false` → `build_scheduler()` 返回 `None`。
- `schedule_cron="不是cron"` → 返回 `None`，且**不抛异常**（服务能启动）。
- 合法 cron → 返回 scheduler，`get_job("daily_pipeline")` 存在，
  其 `trigger` 的下一个触发时间在**下一个符合表达式的时刻**
  （用固定假时间构造，或对 `CronTrigger.from_crontab("15 19 * * 1-5")` 直接断言
  `get_next_fire_time(now)` 的时分 == 19:15，避开跨天断言的脆弱性）。
- `_daily_job` 单测：mock `manager.submit` 返回假 record → 断言以
  `triggered_by="scheduler"`、`kind=DAILY`、`params={"push": True}` 调用。
- `_daily_job`：`submit` 抛 `TaskAlreadyRunning` → 不向外冒异常（调度器不被搞崩）。
- 端到端（patch `pipeline.run_daily`）：手工 `scheduler.add_job(_daily_job, DateTrigger(现在))`
  触发一次，断言 `tasks.db` 里出现一条 `triggered_by="scheduler"` 的任务。
- 单飞集成（**本任务最重要的测试**）：先 `submit` 一个卡住的 DAILY 任务，
  再调 `_daily_job` → 断言只产生 1 条任务记录，第二条被跳过并记 WARNING
  （`caplog` 断言中文文案）。这条守住"调度与手动并发时不会双跑 baostock"。

## 6. 验收标准

```bash
ruff check sequoia_x
pytest -q
# 手工联调（测试专用配置，跑完改回）：
SCHEDULER_ENABLED=true SCHEDULE_CRON="*/2 * * * *" 启动服务，等待 2~3 分钟
GET /api/info → scheduler_enabled=true, scheduler_running=true
GET /api/tasks → 出现 triggered_by="scheduler" 的记录
```

逐条自查：

1. **默认 `.env.example` 下 `SCHEDULER_ENABLED=false`**（约束 §6，最重要的一条）。
2. cron 写错时服务仍然正常启动并可查询（不是 500、不是启动失败）。
3. 调度路径没有直接调用 `pipeline`（`grep -rn "run_daily\|run_backfill" sequoia_x/scheduler/`
   只允许出现在 patch/import 说明中，不允许真实调用）。
4. 调度器时间戳与 `task_run.created_at` 时区一致（看实际落库值确认）。
5. 若改了 `pipeline.run_daily` 的 `synced_rows == 0` 分支，CLI 行为是否仍与
   改造前一致（`python main.py` 在休市日跑会打印新 WARNING 并快速结束 —— 这是
   **刻意的行为变更**，必须在汇报里显式标出并告知需求方）。
6. 手工联调若真触发过飞书推送，汇报里说明推送了几次、内容是什么（约束 §6）。

## 7. 完成后汇报

- APScheduler 实际装到的版本（3.x 还是被解析成别的）
- `ZoneInfo("Asia/Shanghai")` 在本机的可用性结论
- 是否动了 `pipeline.py`，diff 全文
- 联调产生的任务记录条数与 `triggered_by` 取值
