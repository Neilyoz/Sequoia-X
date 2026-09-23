# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层使用 [baostock](http://baostock.com)（免费、无需注册）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。baostock 的风控盯的是**新建连接频率**而非
请求量，因此数据同步固定为单进程单连接，详见[数据说明](#数据说明)。

---

## 运行模式

```bash
python main.py                  # 日常模式：单进程串行增量补数据 + 跑策略 + 飞书推送
python main.py --backfill       # 回填模式：全市场历史K线一次性灌入（约12分钟）
python main.py --names          # 只同步股票名称（数据源 baostock）
python main.py --reset-baostock # 清除 baostock 熔断状态（换出口 IP 后用）
```

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写飞书 Webhook URL
```

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

约 12 分钟完成 ~5200 只 A 股历史后复权日 K 数据回填。

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   └── rps_breakout.py      # RPS 突破策略
│   └── notify/
│       └── feishu.py            # 飞书 Webhook 推送
└── tests/                       # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册）
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：单进程串行通过 baostock 拉取，一轮跑批只新建 1 条 TCP 连接

### 股票列表（stock_basic）的填充策略

`stock_basic` 存全市场代码与名称，是回填清单与飞书卡片显示名的来源。数据源**只有
baostock 一个**，`query_stock_basic` 一次同时返回代码与名称，只收「上市 + 股票」两类：

| 环节 | 取数方式 | baostock 不可用时 |
|---|---|---|
| 全市场代码 + 名称 | `query_stock_basic`（唯一源） | 日常链路跳过，回填链路退回本地已入库代码 |
| 回填（`--backfill`） | 同上 | 退回本地已入库代码（保住流程，新股下一轮再补） |
| 日常名称刷新 / `--names` | 同上 | 跳过，不回退本地 |

三个环节都用 `DataEngine.sync_stock_basic()`（取数抽在 `_fetch_basic_from_baostock()`），
日常链路传 `return_local_on_failure=False`。

要点：

- 数据源已收敛为 baostock 单源，**不再有 akshare 兜底**（见下节「为什么只有 baostock」）。
- 日常链路**不**用本地历史代码兜底：那一轮必须拿到当日真实清单，拿不到就让飞书卡片退回
  显示雪球代码，好过用陈年清单选出过时信号。
- 回填链路保留本地回退：它只是「补历史」，用旧清单补也比整轮空转强。

### 当日行情（stock_daily）的同步策略

日常增量的唯一数据源是 baostock：一次会话、单条 TCP 连接拉完全市场，单轮跑批只
`bs.login()` 一次。

baostock 不可用时**本轮无增量**（返回 0），不产任何数据。这是刻意的取舍：异源数据
口径不同（后复权基准不一致，实测同一日价格可差 30% 以上），宁可空跑也不写入拉歪
均线/通道的脏数据。

其他取舍：

- 一次日历查询（`query_trade_dates`）确定目标交易日：休市日直接跳过，省掉 5200 次
  必然为空的逐只查询。
- 停牌 / 无数据的股票直接跳过，不写空行。
- 本轮无增量时，日常跑批走「无新增行情（可能非交易日）」分支安全跳过策略，不误推飞书。

### 为什么只有 baostock

项目早期用 akshare（东方财富接口）作兜底，后来全部移除，原因：

1. **口径漂移**：akshare 的后复权基准与 baostock 不同，异源混写必然污染指标，对齐
   逻辑复杂且脆弱。
2. **反爬对抗**：东财对高频访问敏感，需要串行 + sleep 节流，稳定性不如 baostock。
3. **单一真相**：一个数据源一套口径，排查问题时不用先确认「这条数据是谁写的」。

`strategy/private_placement.py`（定增公告策略）原本依赖 akshare 的增发公告接口，
baostock 无对应能力，该策略已**停用**（`run()` 恒返回空列表）。

### 代理环境注意

项目的网络请求**不主动读代理**（代码里没有 `proxy` / `trust_env` 处理），走系统默认
出口。若本机装了 Clash 之类的代理工具，确认 `public-api.baostock.com:10030`
（baostock API）能走通，否则行情与列表会同时失效。

```bash
# 查系统代理
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer
# 查环境变量代理（优先级更高，会覆盖系统代理）
echo $HTTP_PROXY $HTTPS_PROXY
```

排查连不上时先看清出口：**环境变量代理 > 系统代理 > 直连**。

### 被 baostock 限流或封禁时怎么办

baostock 的反滥用盯的是**新建连接频率**，不是请求量 —— 每次 `bs.login()` 都会新建一条
TCP 连接。历史版本用 8 进程并行 + 断线自动重连，单轮跑批能打出上百条连接，会被服务端
拉黑。

封禁发生在**网络层**：API 服务器（`public-api.baostock.com:10030`）直接丢弃该 IP 的
数据包，表现是连接超时（`10002007 网络接收错误`）而不是某个错误码。此时官网能正常
打开、换成其他网络也能连上，说明是**源 IP 被封**，换出口 IP 即可绕过：

```bash
# 1. 换出口 IP：宽带重新拨号、切手机热点，或走代理出口
# 2. 清除本地熔断状态，让下一轮跑批立刻重试
python main.py --reset-baostock
```

程序检测到不可达时会写盘熔断（`data/baostock_breaker.json`），冷却窗口 10 分钟起、
指数增长、6 小时封顶。冷却期内跑批**不再发起任何连接**，避免把服务端的封禁计时刷新。

---

## 许可证 | License

MIT
