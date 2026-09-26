/**
 * 后端接口的类型镜像。
 *
 * 字段名与 `sequoia_x/api/schemas.py` 的 pydantic 模型逐字对应（架构 §4 接口全表是契约源），
 * 任何一边改名都会在这里立刻暴露成编译错误 —— 这是刻意不做 camelCase 转换的原因：
 * 少一层映射就少一处"前端以为有、后端其实没有"的错觉。
 */

/** GET /api/signals 的单条信号（SignalItem）。`name` 为 null 表示行情库未收录该代码。 */
export type SignalItem = {
  trade_date: string;
  strategy: string;
  webhook_key: string;
  symbol: string;
  name: string | null;
  xueqiu_code: string;
};

/** GET /api/signals 的分页信封（SignalListResponse）。`total` 是过滤后的总条数。 */
export type SignalListResponse = {
  items: SignalItem[];
  total: number;
  limit: number;
  offset: number;
};

/** GET /api/market/stocks 的股票基础信息。 */
export type StockListItem = {
  symbol: string;
  name: string;
};

/** GET /api/market/stocks 的分页信封。 */
export type StockListResponse = {
  items: StockListItem[];
  total: number;
  limit: number;
  offset: number;
};

export type StockListQuery = {
  keyword?: string;
  limit: number;
  offset: number;
};

/** K 线视图可选择的本地日线条数。 */
export type OhlcvLimit = 60 | 120 | 250 | 500;

/** GET /api/market/{symbol}/ohlcv 的日线数据。 */
export type OhlcvItem = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  turnover: number;
};

export type OhlcvResponse = {
  symbol: string;
  xueqiu_code: string;
  items: OhlcvItem[];
  total: number;
};

/** GET /api/strategies 的条目（StrategyInfo）。后端只给类名与 webhook_key，无中文名。 */
export type StrategyInfo = {
  name: string;
  webhook_key: string;
  has_dedicated_webhook: boolean;
};

/**
 * GET /api/auth/me 的 mode（`api/auth.py:AuthContext` 的三态，是对外契约）：
 * open=服务端未配 API_KEY（本地开发），session=cookie 已鉴权，apikey=凭头放行。
 */
export type AuthMode = "open" | "session" | "apikey";

export type MeResponse = {
  authenticated: boolean;
  mode: AuthMode;
};

/** POST /api/auth/login 成功响应；cookie 在 Set-Cookie 头里，body 不含 token。 */
export type LoginResponse = {
  authenticated: boolean;
  expires_in: number;
};

/** GET /api/info 的服务自述（InfoResponse）；页脚只用 version，其余字段照契约声明。 */
export type InfoResponse = {
  name: string;
  version: string;
  timezone: string;
  scheduler_enabled: boolean;
  scheduler_running: boolean;
  schedule_cron: string;
  strategy_count: number;
  active_task_id: string | null;
  db_path: string;
  started_at: string;
  auth_mode: string;
  active_sessions: number;
  frontend_served: boolean;
};

/**
 * 看板的信号查询参数。
 *
 * 日期一律走 `start`/`end` 闭区间：后端 `date` 与 `start`/`end` 同时给会 400
 * `conflicting_filters`（`routes/queries.py:list_signals`），所以这里压根不声明 date。
 * `strategy` 后端只接受**单个**值（类名或 webhook_key 等价），不支持数组。
 */
export type SignalQuery = {
  start: string;
  end: string;
  strategy?: string;
  symbol?: string;
  limit: number;
  offset: number;
};
