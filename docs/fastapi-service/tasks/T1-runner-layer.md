# T1 · 抽取 runner 编排层与策略注册表

**依赖**：无（第一个任务）　**阻塞**：T2、T3、T4、T5
**规模**：约 4 个新文件 + 改 2 个文件 + 2 个测试文件

## 1. 目标

把 `main.py:45-108` 内联的跑批编排逻辑抽成可复用、可测、无副作用退出语义的
`sequoia_x/runner/` 包，让 CLI 与后续 API 共用同一条业务链路。
顺手把"新增策略要在 main.py 追加一行"改成注册表单点维护。

**本任务不引入 FastAPI，不引入线程，不引入新库表。** 只做搬迁和收口。

## 2. 动手前先读

- `main.py` 全文（112 行）—— 这是要搬迁的源头
- `sequoia_x/strategy/base.py` —— `run() -> list[str]` 契约与 `webhook_key`
- `sequoia_x/core/config.py:74-92` —— `get_settings` 单例
- `tests/test_main.py` —— 你不能破坏的契约
- 约束 [§1 §2 §3](../01-constraints.md)

## 3. 文件清单

新建：

| 文件 | 内容 |
| --- | --- |
| `sequoia_x/core/bootstrap.py` | `bootstrap()`，见架构 §3.1 |
| `sequoia_x/core/__init__.py` | 已有，不动 |
| `sequoia_x/runner/__init__.py` | 中文 docstring：`"""编排层：组合数据引擎、策略与通知器，提供 CLI 与服务端共用的跑批流程。"""` |
| `sequoia_x/runner/registry.py` | 策略注册表 |
| `sequoia_x/runner/pipeline.py` | `run_daily()` / `run_backfill()` + 两个 report dataclass |
| `tests/test_bootstrap.py` | bootstrap 幂等性 |
| `tests/test_runner_registry.py` | 注册表完整性与 `resolve` |
| `tests/test_runner_pipeline.py` | 跑批编排的等价性与容错 |

修改：

- `main.py` —— 瘦身为薄壳

`data/*.db` 已在 `.gitignore:20` 内，新库 `data/tasks.db` 自动被忽略，**无需改 `.gitignore`**。

禁止改动：`sequoia_x/strategy/**`、`sequoia_x/data/**`、`sequoia_x/notify/**`、
`sequoia_x/core/config.py`（新配置字段留给 T2/T3 加，避免与它们的验收顺序打架）、
`tests/test_main.py`、`pyproject.toml`。

## 4. 实现要点

### 4.1 `bootstrap.py`

```python
"""启动期副作用统一收口。

必须在 import 任何 sequoia_x 业务子模块之前调用，原因见 docs/fastapi-service/01-constraints.md §1。
"""
```

- 模块级 `_bootstrapped: bool` 标志保证幂等。
- 内部 `from dotenv import load_dotenv` 后 `load_dotenv()`；
  `socket.setdefaulttimeout(60.0)`。
- **`60.0` 旁边必须保留原 `main.py:17-19` 那条解释性注释**（baostock 裸 socket 继承单次
  recv 超时、全市场列表 60~72 秒）。这条注释是防回归的唯一屏障。
- `bootstrap()` 内部**不得 import** `sequoia_x.core.config`，否则调用方无法保证顺序。
- 不返回值，不抛异常（`load_dotenv` 找不到 `.env` 时静默即可，现有行为如此）。

### 4.2 `registry.py`

- 顶部 import 七个策略类，`STRATEGY_CLASSES` 顺序**严格**取 `main.py:70-78` 现有顺序。
- `resolve()` 接受的名称形式：类名精确匹配（`MaVolumeStrategy`）、
  类名小写（`mavolumestrategy` 不要求，只实现 `ma_volume_strategy` 下划线形式与精确类名）、
  `webhook_key`（`ma_volume`、`turtle`、`flag`、`shakeout`、`limit_down`、`rps`、
  `private_placement`）。实现方式：构建 `STRATEGY_KEYS` 时同时登记
  `cls.__name__` 与 `cls.webhook_key`（都 `.lower()` 后作 key）。
- 未知名称：抛 `UnknownStrategyError`，异常消息列出全部合法名称，方便排查。
- 去重：`resolve(["ma_volume","MaVolumeStrategy"])` 只能返回一个 `MaVolumeStrategy`。
- `get_all_strategies()` 返回**新列表**，不要暴露内部 tuple 的引用。

### 4.3 `pipeline.py`

`run_daily()` 的步骤与日志文案照抄 `main.py`，按下述骨架：

```python
def run_daily(settings, *, push=True, strategies=None, use_multiprocessing=True) -> DailyReport:
    logger = get_logger(__name__)
    engine = DataEngine(settings)          # 每次跑批自建，不缓存
    logger.info("开始拉取最新快照...")
    count = engine.sync_today_bulk()
    logger.info(f"快照同步完成，写入 {count} 只股票")

    outcomes: list[StrategyOutcome] = []
    notifier = FeishuNotifier(settings, engine)
    for cls in resolve(strategies):
        strategy = cls(engine=engine, settings=settings)
        name = cls.__name__
        logger.info(f"执行策略：{name}")
        try:
            selected = strategy.run()
        except Exception as exc:
            logger.exception(f"{name} 执行异常，跳过该策略")
            outcomes.append(StrategyOutcome(name, strategy.webhook_key, [], False,
                                            f"{type(exc).__name__}: {exc}"))
            continue
        logger.info(f"{name} 选出 {len(selected)} 只股票")
        pushed = False
        if selected and push:
            notifier.send(symbols=selected, strategy_name=name,
                          webhook_key=strategy.webhook_key)
            pushed = True
        elif selected:
            logger.info(f"{name} 有选股结果，但 push 已关闭，跳过推送")
        else:
            logger.info(f"{name} 无选股结果，跳过推送")
        outcomes.append(StrategyOutcome(name, strategy.webhook_key, selected, pushed))

    return DailyReport(trade_date=date.today().strftime("%Y-%m-%d"),
                       synced_rows=count, outcomes=outcomes, push_enabled=push)
```

- `use_multiprocessing` 参数：**本任务只透传不消费**。若 `DataEngine.sync_today_bulk()`
  当前无该参数，则 `run_daily` 内部保留同名参数但在调用处不加参数，并加中文注释说明
  "服务态降级开关由 T2 实测 Pool 后接入"。**不要在这一步改 `engine.py`**（约束 §4.3）。
- `run_backfill()`：照抄 `main.py:56-62` 两步（`sync_stock_basic()` → `backfill(...)`），
  日志文案保留 `进入回填模式...` 与 `Sequoia-X V2 回填模式运行完成`。
- 两个函数都**不得** `sys.exit`、不得 `print`、不得调 `get_settings()`。
- `DailyReport.to_dict()` 提供 JSON 可序列化视图（T2 落库要用），字段名与 dataclass 一致，
  `outcomes` 展开为 dict 列表。

### 4.4 `main.py` 薄壳

目标形态（保留必需的名字与顺序）：

```python
"""Sequoia-X V2 主程序入口（CLI）。

  python main.py              # 日常模式
  python main.py --backfill   # 回填模式
服务化入口见 sequoia_x/api/app.py，两者共用 sequoia_x/runner 编排层。
"""
import argparse
import sys

from sequoia_x.core.bootstrap import bootstrap

bootstrap()

from sequoia_x.core.config import get_settings        # ← 名字必须留在 main 模块命名空间
from sequoia_x.core.logger import get_logger
from sequoia_x.runner import pipeline


def main() -> None:
    ...
    logger = None
    try:
        settings = get_settings()
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")
        if args.backfill:
            pipeline.run_backfill(settings)
            return
        report = pipeline.run_daily(settings)
        logger.info(f"Sequoia-X V2 运行完成，共选出 {report.total_signals} 个信号")
    except Exception:
        # 原样保留 main.py:99-106 的双重兜底：logger 不可用时退回 traceback.print_exc
        ...
        sys.exit(1)
```

- 保留 `main.py:99-106` 那段"logger 初始化本身可能失败 → 退回 traceback"的兜底逻辑。
  它存在是有原因的，不要"简化"掉。
- 原代码 `main.py:108` 的收尾日志引用了 try 块内才绑定的 `logger`。现有控制流下它不会
  触发 `UnboundLocalError`（异常路径已在 except 里 `sys.exit`），但薄壳里仍显式
  `logger = None` 初始化——不为修 bug，只为重构后控制流变动时不引入。
  除此之外不要"顺手优化"这段兜底逻辑。
- 顶部文档字符串的两种模式说明保留并补一句服务化入口。

## 5. 测试要求

`tests/test_bootstrap.py`
- 连续调用 `bootstrap()` 两次不抛异常（幂等）。
- 调用后 `socket.getdefaulttimeout() == 60.0`。

`tests/test_runner_registry.py`
- 属性测试：对注册表中任意策略，`resolve([别名])` 与 `resolve([类名])` 结果相同（去重生效）。
- `get_all_strategies()` 长度 == 7，且**逐一断言类名与顺序**（防有人调整顺序）。
- 每个策略的 `webhook_key` 与 `.env.example` 里的 `STRATEGY_WEBHOOK_*` 键集合一致
  （读 `sequoia_x.strategy` 类属性即可，不要读文件）。
- `resolve(["不存在的名字"])` 抛 `UnknownStrategyError`，消息含合法名称列表。
- `resolve(None)` 等价于 `get_all_strategies()`。

`tests/test_runner_pipeline.py`（全部 mock，不碰网络与真实库）
- mock `DataEngine` 与 `FeishuNotifier`（patch `pipeline` 模块里的名字，
  所以要 `from sequoia_x.data.engine import DataEngine` 形式引入）。
  给假 engine 让两个策略返回非空、一个返回空 →
  断言 `send()` 只对非空调用、`webhook_key` 路由正确、`pushed` 标志正确。
- `push=False` → 断言 `send()` 零调用，但 `outcomes` 里 `symbols` 仍完整（跑批不推也要能查询）。
- 让某策略 `run()` 抛 `ValueError` → 断言整批不中断、其余策略正常、失败项 `error` 非空。
- 断言 `run_daily` **不会** `sys.exit`（不需要专门测，靠"函数正常返回"隐含覆盖）。

## 6. 验收标准

```bash
ruff check sequoia_x main.py
pytest -q                                   # 必须全绿，含未修改的 tests/test_main.py
python -c "import main"                     # import 不炸
python main.py --help                       # argparse 签名未变
```

逐条自查：

1. `git diff tests/test_main.py` 为空。
2. `main.py` 里仍有 `from sequoia_x.core.config import get_settings`，且在 `try:` 内被调用。
3. `grep -rn "sys.exit\|print(" sequoia_x/runner/` 无结果。
4. `grep -n "setdefaulttimeout" sequoia_x/core/bootstrap.py` 有结果，且 `main.py` 里**不再有**
   第二处 `setdefaulttimeout` 或 `load_dotenv`。
5. 注册表七个类顺序与 `git show HEAD:main.py` 里的一致。
6. 真实跑一次 `python main.py`（联网、会推飞书）**不是本任务要求**；
   若你确实跑了，必须先征得需求方同意（外部副作用，约束 §6）。

## 7. 完成后汇报

- 变更文件清单 + 各自行数
- 三个测试文件的 pytest 输出摘要
- 你对 `use_multiprocessing` 透传方式的处理（T2 依赖这个结论）
- 发现的与文档不一致之处
