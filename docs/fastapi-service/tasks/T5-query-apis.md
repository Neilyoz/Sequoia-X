# T5 · 选股结果与行情查询接口

**依赖**：T3　**可与 T4 并行**（注意 `app.py` 的 router 注册点，见 T4 §3）
**规模**：1 个新路由文件 + 1 个 schema 追加 + 1 个测试文件

## 1. 目标

把服务变成可查询的数据源：历史选股信号、单次任务信号明细、日线行情、股票基础信息。
这一层让"API 形态"不只是"能远程触发脚本"，而是有实际查询价值。

本任务交付：`GET /api/tasks/{task_id}/signals`、`GET /api/signals`、
`GET /api/market/{symbol}/ohlcv`、`GET /api/market/{symbol}/basic`。

## 2. 动手前先读

- 约束 [§5 SQLite 读写规则](../01-constraints.md)（**必读，本任务全在这条约束里行走**）
- 架构 §4 接口全表、§3.5 `TaskStore.query_signals` 签名
- `sequoia_x/data/engine.py:158-165`（`get_ohlcv`，**无 LIMIT、无日期范围，全表扫单只**）
  与 `get_stock_names` / `get_local_symbols`
- `sequoia_x/notify/feishu.py:37-44` 的 `_to_xueqiu_code`（响应里带雪球代码，复用同一套映射）

## 3. 文件清单

新建：`sequoia_x/api/routes/queries.py`、`tests/test_api_queries.py`
修改：`sequoia_x/api/schemas.py`（追加响应模型）、`sequoia_x/api/app.py`（注册 router，
**只加一行 `include_router`**）
可选修改：`sequoia_x/data/engine.py` —— 仅允许**新增只读查询方法**（§4.2），
**严禁修改任何现有方法**。

禁止改动：`main.py`、`sequoia_x/runner/**`、`sequoia_x/task/manager.py`、
`sequoia_x/notify/**`、`sequoia_x/strategy/**`、`sequoia_x/scheduler/**`。

## 4. 实现要点

### 4.1 symbol 参数校验（安全点）

`/api/market/{symbol}/ohlcv` 的 `symbol` 会进 SQL。现有 `get_ohlcv` 用了参数化查询
（`engine.py:160-164`，`?` 占位），**没有 SQL 注入面**，但仍然要校验格式，
因为它同时决定文件名/日志/上游映射：

- 路由层 `symbol: str` + 显式校验：`^\d{6}$`，不匹配 → 400
  `detail={"code":"invalid_symbol"}`。
- 校验函数放 `queries.py` 内部或 `schemas.py` 里做 pydantic 模式，
  **不要**在 `DataEngine` 里做校验（数据层不接受业务校验，那是入口层职责）。

### 4.2 行情查询必须加边界

现有 `DataEngine.get_ohlcv(symbol)` 返回该股票**全部**历史行（单只可达数千行）。
一个接口全量返回是可用的但浪费，且 `limit` 是约束 §5 的硬要求。

**新增只读方法**（放在 `engine.py`，紧邻 `get_ohlcv`，命名与 docstring 风格对齐）：

```python
def get_ohlcv_range(self, symbol: str, *, start: str | None = None,
                    end: str | None = None, limit: int = 250) -> pd.DataFrame:
    """按日期区间倒序取至多 limit 条日线（只读，不触发任何网络同步）。"""
```

- SQL 用 `WHERE symbol = ?` + 可选 `AND date >= ?` / `AND date <= ?`
  + `ORDER BY date DESC LIMIT ?`，全部参数化。
- 返回前按 `date` **升序**重排（倒序取是为了拿最新的，但响应给人看要正序）。
- `limit` 上限 `500`（在 pydantic 模型里 `le=500` 拦，数据层再 `min(limit, 500)` 兜底）。
- 不新增索引（`idx_symbol_date` 已够用），**不要**为了这个查询改 `stock_daily`。
- `start`/`end` 必须是 `YYYY-MM-DD`，非法格式 → 400，不要塞进 SQL 让 SQLite 静默比错。

`basic` 用 `get_stock_names([symbol])`（现成方法，读 `stock_basic` 表）。

### 4.3 响应模型

```python
class SignalItem(BaseModel):
    trade_date: str
    strategy: str
    webhook_key: str
    symbol: str
    name: str | None            # 来自 stock_basic，查不到为 null
    xueqiu_code: str            # 复用 SH/SZ/BJ 映射规则

class OhlcvItem(BaseModel):
    date: str; open: float; high: float; low: float
    close: float; volume: float; turnover: float
```

- `GET /api/signals` → `{"items":[SignalItem...], "total":int, "limit":int, "offset":int}`，
  query：`date`（`YYYY-MM-DD`，单日）、或 `start` + `end`（**闭区间**，供前端看板的
  "近 7/30 天"筛选使用，见 [03 §6](../03-frontend-and-auth.md)）、
  `strategy`（类名或 webhook_key 都接受）、`symbol`、
  `limit`（默认 100，`le=1000`）、`offset`。
  `date` 与 `start`/`end` **同时给时以 `date` 为准并返回 400**
  `detail={"code":"conflicting_filters"}`（静默取舍会让前端调试时抓不到为什么结果不对）。
  **日期类参数至少要给一个**，否则等于全表扫，返回 400
  `detail={"code":"missing_filter"}`（这条防误用，写进 OpenAPI description）。
  注意 `signal` 表按 `trade_date` 存文本日期，`YYYY-MM-DD` 形式的字符串区间比较与时间序一致，
  可直接用 `BETWEEN ? AND ?` 参数化查询。
- `GET /api/tasks/{id}/signals` → 同样 items 列表，不分页（单任务最多几十条）。
- `name` 的补齐：一次 `get_stock_names(去重后的 symbols)`，**严禁 N+1**
  （不要循环里逐只查）。`stock_basic` 是小表，一次 `IN` 查询足够。
  `get_stock_names` 现接受列表，确认其实现能处理空列表（空则跳过查询直接返回 `{}`）。
- 排序：`ORDER BY trade_date DESC, strategy, symbol`，保证同一查询结果稳定可分页
  （**分页结果不稳定是隐蔽 bug**：SQLite 无 `ORDER BY` 时同 offset 可能返回不同行）。

### 4.4 只读，绝对不触发同步

- 这些路由**只读本地库**，不得有任何 baostock 调用。
  理由：约束 §3（并发踩全局 socket）+ 查询接口必须是廉价、可高频调的。
- 明确"数据新鲜度"：`GET /api/market/{symbol}/ohlcv` 响应里带
  `"note": "只读本地缓存，最新日期 {max_date}"`？ —— **不做**。
  改为：如果 `stock_daily` 为空，返回 409 `detail={"code":"database_not_seeded",
  "hint":"本地无行情数据，请先 POST /api/tasks/backfill 或执行 python main.py --backfill"}`。
  空库最容易被误认为"接口坏了"，这条提示能省掉一轮排查。

### 4.5 依赖与鉴权

`queries.py` 的 `APIRouter(dependencies=[Depends(require_api_key)])`（沿用 T3 的 `deps.py`，
**不要重写鉴权逻辑**）。挂到 `/api` 前缀下。

## 5. 测试要求

`tests/test_api_queries.py`（用 `tmp_path` 下的独立小库，**绝不碰 `data/sequoia_v2.db`**）

fixture：临时建 `stock_daily` + `stock_basic`，插入 3 只股票 × 5 天数据 + 若干信号行。

- `GET /api/signals?date=...` 返回正确条数、`total` 正确、`name` 已补齐。
- `GET /api/signals?start=2026-01-01&end=2026-01-31` 命中**闭区间**边界
  （造数据时特意在 `start` 与 `end` 当天各放一条，断言两条都在结果里）。
- `GET /api/signals`（无过滤）→ 400 `missing_filter`；
  `date` 与 `start` 同时给 → 400 `conflicting_filters`。
- `strategy` 用类名和用 `webhook_key` 两种写法结果一致。
- 分页：`limit=2` 遍历全部，各页**无重复无遗漏**（把 items 拼起来与不分页结果比较）。
- `GET /api/market/000001/ohlcv` 默认返回升序、`limit` 生效；
  `?start=&end=` 区间正确；`?limit=9999` → 422。
- `GET /api/market/12345/ohlcv`（5 位）→ 400 `invalid_symbol`；
  `GET /api/market/abcdef/ohlcv` → 400。
- SQL 注入探针：`symbol="000001'; DROP TABLE stock_daily; --"` → 400 且
  之后 `stock_daily` 表仍在（守 §4.1）。
- 空库时 `/api/market/000001/ohlcv` → 409 `database_not_seeded`。
- `xueqiu_code` 映射：`600519→SH600519`、`000001→SZ000001`、`830799→BJ830799`
  （与 `feishu.py:37-44` 的行为一致，避免同一代码在卡片和 API 里两种写法）。
- 只读保证：断言这些路由不 import 也不调用 `sync_*` 方法
  （`patch` 掉 `DataEngine.sync_today_bulk` 并断言 `called == False`）。

## 6. 验收标准

```bash
ruff check sequoia_x
pytest -q
```

手工（真实 456 MB 库，只读安全）：

```bash
curl -s -H "X-API-Key: <key>" "http://127.0.0.1:8123/api/market/000001/ohlcv?limit=3"
curl -s -H "X-API-Key: <key>" "http://127.0.0.1:8123/api/signals?date=<最近一个有信号日>"
```

逐条自查：

1. `data/sequoia_v2.db` 的 mtime 与文件哈希**未变化**（只读证明；用 `sha256sum` 前后对比）。
2. `git diff sequoia_x/data/engine.py` 只有新增方法，无现有方法改动
   （`git diff --stat` + 逐行读 diff）。
3. 所有列表接口都有 `LIMIT` 兜底。
4. `/api/signals` 无过滤时确实拒绝。
5. `tests/test_data_engine.py` 仍全绿（证明没改坏数据层）。
6. 响应里没有任何 `iterrows` / 全表 `pd.read_sql("SELECT * FROM stock_daily")` 之类
   会把 456 MB 拉进内存的写法（`grep -n "SELECT \*" sequoia_x/api/` 应无危险结果）。

## 7. 完成后汇报

- 新增/修改的 SQL 语句全文（供复核只读性）
- 真实库上的响应耗时量级（`/api/market/.../ohlcv?limit=250` 大概几毫秒）
- `get_stock_names` 对空列表与不存在代码的实际行为
- 是否发现 `stock_daily` 缺索引导致的慢查询（若有，**报告但不要自行加索引**）
