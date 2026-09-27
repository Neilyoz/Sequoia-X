/**
 * 全站唯一的接口出口：所有 /api 调用只能从这里走（任务书 §4.2）。
 *
 * 三条硬规则：
 * 1. **一律相对路径**。`BASE` 是空串不是巧合 —— 生产与 API 同源（:8000），
 *    开发期靠 next dev 的 rewrites 把 /api/* 转给 :8000（:3000）。写死绝对地址
 *    会让生产环境立刻坏，也过不了 §6 的 grep 自查。
 * 2. **401 只在这里处理**。组件里不许散落 `if (status === 401)`：跳登录页的判断
 *    依赖"当前是否已在 /login"，散出去必然长出登录页自己 401 后无限重载。
 * 3. **错误码 → 中文文案集中在这里**，组件只拿到人能看懂的句子。
 */

import type {
  InfoResponse,
  LoginResponse,
  MeResponse,
  OhlcvLimit,
  OhlcvResponse,
  SignalListResponse,
  SignalQuery,
  StockListQuery,
  StockListResponse,
  StrategyInfo,
  TaskResponse,
  TaskSignalsResponse,
} from "./types";

/** 相对路径基准：留空即"当前源"，开发（:3000）与生产（:8000）两种形态都成立。 */
const BASE = "";

/** CSRF 第二道防线（03 §4）：cookie 模式下的写操作必须带这个头，后端 `require_write_guard` 校验。 */
export const CLIENT_HEADER = { "X-Sequoia-Client": "web" } as const;

/** 后端会出现的 detail.code（架构 §4 响应约定 + T5/T7 实现）。 */
export type ApiErrorCode =
  | "invalid_credentials"
  | "unauthorized"
  | "auth_disabled"
  | "login_rate_limited"
  | "missing_client_header"
  | "missing_filter"
  | "conflicting_filters"
  | "invalid_date"
  | "invalid_symbol"
  | "unknown_strategy"
  | "task_already_running"
  | "database_not_seeded"
  | "stock_list_not_seeded"
  | "internal_error"
  | "not_found"
  | "unknown";

/**
 * 统一的接口异常。
 *
 * `code` 是机器可读的后端错误码（拿不到就是 `unknown`），`message` 是给人看的中文。
 * 组件只需要读 `code`/`message`，不必理解 HTTP 语义。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;

  constructor(status: number, code: ApiErrorCode, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** 错误码 → 中文文案。新增码只改这张表，别在组件里补 if。 */
const ERROR_TEXT: Record<ApiErrorCode, string> = {
  invalid_credentials: "API Key 不正确",
  unauthorized: "登录状态已失效，请重新登录",
  auth_disabled: "服务未启用鉴权（未配置 API_KEY），无需登录",
  login_rate_limited: "尝试次数过多，请稍后再试",
  missing_client_header: "浏览器请求缺少来源标识，请刷新页面后重试",
  missing_filter: "请至少选择一个筛选条件",
  conflicting_filters: "日期条件冲突，请刷新页面后重试",
  invalid_date: "日期格式不正确，应为 YYYY-MM-DD",
  invalid_symbol: "股票代码格式不正确，应为 6 位数字",
  unknown_strategy: "策略名称无法识别，请刷新后重选",
  task_already_running: "已有跑批任务在执行，请稍后再试",
  database_not_seeded: "本地行情库尚未回填",
  stock_list_not_seeded: "本地股票清单尚未同步，请先同步股票名称",
  internal_error: "服务端内部错误，请稍后重试",
  not_found: "接口或资源不存在",
  unknown: "请求失败，请稍后重试",
};

/** 「未回填」错误码：看板据此渲染第四种状态（专用文案 + 下一步指引）。 */
export const NOT_SEEDED_CODE: ApiErrorCode = "database_not_seeded";

/**
 * 这个错误是否已经由 API 层发起"跳登录页"？
 *
 * 组件需要它，但**不需要**自己判断状态码：跳转发生在 `send()` 里，浏览器换页要几百毫秒，
 * 这段时间组件如果按"出错"渲染，就会先闪一屏红色报错再进登录页（任务书 §4.3 明确要避免）。
 * 于是把判据以函数形式导出，状态码语义仍然只有一处实现。
 */
export function isRedirectingToLogin(error: unknown): boolean {
  return error instanceof ApiError && error.code === "unauthorized" && !onLoginPage();
}

const KNOWN_CODES: readonly ApiErrorCode[] = [
  "invalid_credentials",
  "unauthorized",
  "auth_disabled",
  "login_rate_limited",
  "missing_client_header",
  "missing_filter",
  "conflicting_filters",
  "invalid_date",
  "invalid_symbol",
  "unknown_strategy",
  "task_already_running",
  "database_not_seeded",
  "stock_list_not_seeded",
  "internal_error",
];

function isKnownCode(raw: string): raw is ApiErrorCode {
  return (KNOWN_CODES as readonly string[]).includes(raw);
}

/**
 * FastAPI 的 `detail` 有两种形态：业务代码抛的是对象 `{"code": "..."}`，
 * 而参数校验失败（422）是**数组**、挂载缺产物时的 404 干脆不是 JSON。
 * 所以这里从 unknown 逐层用类型守卫收窄，全程不出现 any。
 */
function extractCode(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) {
    return null;
  }
  const detail: unknown = (payload as { detail?: unknown }).detail;
  if (typeof detail !== "object" || detail === null) {
    return null;
  }
  const code: unknown = (detail as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

function toApiError(status: number, payload: unknown): ApiError {
  const raw = extractCode(payload);
  if (raw !== null && isKnownCode(raw)) {
    return new ApiError(status, raw, ERROR_TEXT[raw]);
  }
  // 没有错误码时按状态码兜底：404 值得单独一句（多半是访问了不存在的路径）。
  if (status === 404) {
    return new ApiError(status, "not_found", ERROR_TEXT.not_found);
  }
  return new ApiError(status, "unknown", ERROR_TEXT.unknown);
}

/** 当前是否停在登录页：登录页自己收到 401 不该再跳自己。 */
function onLoginPage(): boolean {
  return window.location.pathname.startsWith("/login");
}

/** 401 的处置：非登录页一律整页换到 /login/（带尾斜杠，匹配 trailingSlash 的产物形态）。 */
function handleUnauthorized(status: number): void {
  if (status === 401 && !onLoginPage()) {
    window.location.replace("/login/");
  }
}

/**
 * 请求参数刻意自定义而不用 `RequestInit`：只有这一处需要 method/header/body 三样，
 * 自定义窄类型能让 header 合并（下面 `headers: {...}` 那行）保持可读且不需要断言。
 */
type RequestOptions = {
  method: "GET" | "POST";
  headers?: Record<string, string>;
  body?: string;
};

/**
 * 唯一的 fetch 调用点。
 *
 * `credentials: "same-origin"` 显式写出（虽然也是默认值）：session cookie 是本站唯一凭据，
 * 这行就是"为什么同源部署不需要配 CORS"的答案。`cache: "no-store"` 防止浏览器把
 * 上一次的信号列表按启发式缓存当成新结果展示。
 * 每个请求都无条件带上 `X-Sequoia-Client`（含 GET）：写操作必须带，读操作带上更省事。
 */
async function send(path: string, options: RequestOptions): Promise<Response> {
  const response = await fetch(`${BASE}${path}`, {
    method: options.method,
    headers: { ...CLIENT_HEADER, ...options.headers },
    ...(options.body === undefined ? {} : { body: options.body }),
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      // 非 JSON 错误体（纯文本 404 等）：payload 留 null，退化成按状态码给文案。
      payload = null;
    }
    handleUnauthorized(response.status);
    throw toApiError(response.status, payload);
  }
  return response;
}

async function requestJson<T>(path: string, options: RequestOptions): Promise<T> {
  const response = await send(path, options);
  return (await response.json()) as T;
}

async function requestNoContent(path: string, options: RequestOptions): Promise<void> {
  await send(path, options);
}

function buildSignalQuery(query: SignalQuery): string {
  const params = new URLSearchParams();
  params.set("start", query.start);
  params.set("end", query.end);
  if (query.strategy) {
    params.set("strategy", query.strategy);
  }
  if (query.symbol) {
    params.set("symbol", query.symbol);
  }
  params.set("limit", String(query.limit));
  params.set("offset", String(query.offset));
  return `/api/signals?${params.toString()}`;
}

function buildStockListQuery(query: StockListQuery): string {
  const params = new URLSearchParams();
  if (query.keyword?.trim()) {
    params.set("keyword", query.keyword.trim());
  }
  params.set("limit", String(query.limit));
  params.set("offset", String(query.offset));
  return `/api/market/stocks?${params.toString()}`;
}

export const api = {
  /**
   * 用 API Key 换 HttpOnly session cookie（03 §3）。
   * login 自身不要求 X-Sequoia-Client（那时还没有会话可言，见 routes/auth.py 注释），
   * 但 send() 统一带上无害，于是省掉一个特例分支。
   */
  login: (apiKey: string): Promise<LoginResponse> =>
    requestJson<LoginResponse>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    }),

  /** 应用初始化：问"我还要不要登录"。401 已由 send() 换成跳 /login/，调用方只管渲染。 */
  me: (): Promise<MeResponse> => requestJson<MeResponse>("/api/auth/me", { method: "GET" }),

  /** 登出：后端返回 204 无响应体（RFC 9110），所以不读 json。 */
  logout: (): Promise<void> => requestNoContent("/api/auth/logout", { method: "POST" }),

  /** 策略下拉选项（注册表顺序）。 */
  strategies: (): Promise<StrategyInfo[]> =>
    requestJson<StrategyInfo[]>("/api/strategies", { method: "GET" }),

  /** 运行选中的策略并关闭飞书推送。 */
  submitDaily: (strategies: string[]): Promise<TaskResponse> =>
    requestJson<TaskResponse>("/api/tasks/daily", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ push: false, strategies }),
    }),

  /** 查询单个跑批任务状态。 */
  task: (taskId: string): Promise<TaskResponse> =>
    requestJson<TaskResponse>(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "GET" }),

  /** 读取指定任务产生的选股信号。 */
  taskSignals: (taskId: string): Promise<TaskSignalsResponse> =>
    requestJson<TaskSignalsResponse>(
      `/api/tasks/${encodeURIComponent(taskId)}/signals`,
      { method: "GET" },
    ),

  /** 历史选股信号查询：保留给其他客户端使用，首页展示股票清单。 */
  signals: (query: SignalQuery): Promise<SignalListResponse> =>
    requestJson<SignalListResponse>(buildSignalQuery(query), { method: "GET" }),

  /** 股票基础列表：本地 stock_basic 搜索和分页。 */
  stocks: (query: StockListQuery): Promise<StockListResponse> =>
    requestJson<StockListResponse>(buildStockListQuery(query), { method: "GET" }),

  /** 单只股票本地日线；limit 只能使用页面提供的范围选项。 */
  ohlcv: (symbol: string, limit: OhlcvLimit): Promise<OhlcvResponse> => {
    const params = new URLSearchParams({ limit: String(limit) });
    return requestJson<OhlcvResponse>(
      `/api/market/${encodeURIComponent(symbol)}/ohlcv?${params.toString()}`,
      { method: "GET" },
    );
  },

  /** 服务自述：页脚版本号。失败按"拿不到就不显示"处理，不干扰看板。 */
  info: (): Promise<InfoResponse> => requestJson<InfoResponse>("/api/info", { method: "GET" }),
};
