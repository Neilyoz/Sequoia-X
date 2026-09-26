# 03 · 前端与登录鉴权设计

> 本篇是**追加范围**。原方案（[README §1](./README.md) 已确认的四项决策）明确排除 Web 页面，
> 需求方后续决定加。追加的决策见下表，**已确认，不再讨论**。

## 1. 追加决策

| 决策点 | 结论 |
| --- | --- |
| 技术栈 | **Next.js 16.3**（锁 `next@16.3.x`），工程目录 **`frontend/`**，TypeScript + Tailwind CSS |
| 部署形态 | **`output: 'export'` 静态导出**，由 FastAPI `StaticFiles` 托管，**与 API 同端口同源** |
| 页面范围 | 一个行情页（+ 必需的登录页）：搜索、分页浏览本地 A 股清单，并在当前页详情区查看所选股票日 K 线与成交量 |
| 鉴权 | **登录页输入 API Key → 换取 HttpOnly session cookie**；`/api/*` 接受 cookie 或 `X-API-Key` 任一 |
| 视觉 | 功能优先，朴素干净。Tailwind + 少量自写组件，**不引入组件库**（shadcn 需要一次性写入大量文件，本次不值） |
| 包管理 | **npm**（Node 24 自带 npm 12），提交 `frontend/package-lock.json`。禁止混用 pnpm/yarn 造出第二份锁文件 |

明确**不做**：仪表盘、跑批控制按钮、任务历史页、策略开关管理、暗色主题切换、
国际化、SSR、Server Actions、WebSocket 实时推送、前端测试框架（Playbook/Jest 等）。

## 2. 为什么是"API Key 换 session"，而不是用户名密码

引入用户名密码要新建一张 `user` 表、要做密码哈希与会话管理，而这是一个**单人自用的选股服务**，
`.env` 里已经有一个只有你知道的 `API_KEY`。复用它作为唯一凭据：

- 零新增凭据，零新增表，零密码哈希与找回逻辑
- `API_KEY` 只存在于服务端 `.env` 与用户的浏览器内存里，**不落到 JS 持久化存储**
- 脚本/`curl` 仍可直接用 `X-API-Key` 头，不必走登录流程 —— 与 T3 的 API Key 语义完全兼容

代价：无法区分多个用户。本项目不需要。

## 3. 会话机制（T7 实现）

```
POST /api/auth/login   {"api_key": "..."}
  → 200 Set-Cookie: sequoia_session=<token>; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800
  → 401  {"detail":{"code":"invalid_credentials"}}      （不回显任何 key 片段）

GET  /api/auth/me      → {"authenticated": true} | 401
POST /api/auth/logout  → Set-Cookie 过期 + 服务端删除会话 → 204
```

- 会话存储：**进程内 `dict[token, expires_at]`**，不落库。理由：单进程部署是本项目的前提
  （约束 §3 决定了不能多副本），进程重启后所有人重新登录一次，这个代价可以接受，
  换来的是"不引入第二张表和清理任务"。
- token：`secrets.token_urlsafe(32)`；比较用 `hmac.compare_digest`。
- TTL：`SESSION_TTL_SECONDS` 默认 `604800`（7 天）。**不设滑动过期**（简单优先）。
- 启动与每次登录时顺手清掉已过期条目，不做后台清理线程。
- 鉴权判定顺序（`deps.authenticate()`）：
  1. `API_KEY` 未配置 → 直接放行（本地开发语义，T3 已有此行为，打 WARNING）
  2. 请求带合法 session cookie → 放行
  3. 请求带正确 `X-API-Key` → 放行
  4. 否则 401

## 4. CSRF

Cookie 鉴权会带来 CSRF 面。三道防线，全部要在 T7 实现并在测试里验证：

1. `SameSite=Strict` —— 跨站请求根本不携带 cookie，这是主力。
2. **写操作**（`POST`/`PUT`/`PATCH`/`DELETE`）要求携带自定义头 `X-Sequoia-Client: web`
   （前端 API client 统一注入）；跨站表单无法设置自定义头，构成第二道证明。
   例外：`POST /api/auth/login` 自身不要求该头（否则要先有 session 才能登录），
   但登录接口本身对 CSRF 无危害。
   `curl` 走 `X-API-Key` 时不要求该头（它是显式鉴权，非 cookie）。
3. 不监听 `0.0.0.0`：`API_HOST` 默认 `127.0.0.1` 保持不变。

## 5. 静态导出与托管（关键坑，务必读完）

### 5.1 rewrites 在 export 模式下不生效

Next.js 的 `rewrites`（dev 期用来把 `/api` 代理到 `http://127.0.0.1:8000`，避免 CORS）
**只在 `next dev` / `next start` 下生效；`output: 'export'` 会忽略并告警**。

因此两个环境的形态不同，**这不是 bug 而是约束**：

| 环境 | 形态 | cookie 同源？ |
| --- | --- | --- |
| 开发 | `uvicorn`(:8000) + `next dev`(:3000)，`next.config.ts` 的 rewrites 把 `/api/*` 转给 8000 | 是（走 3000） |
| 生产 | `next build` → `frontend/out` → FastAPI 挂载在 `/`，浏览器只访问 8000 | 是（同一个端口） |

两条路径都同源，所以**前端代码里一律用相对路径 `/api/...`**，
禁止出现 `http://localhost:8000` 之类的绝对地址（否则生产立刻坏）。
这条要写成 lint 规则或测试断言（T8）。

### 5.2 静态导出对路由形态的限制

- 只有 `generateStaticParams` 预渲染过的动态路由才能被 export 成 HTML。
  → **本次路由就两个：`/`（看板）和 `/login`。不要用 `[id]` 这类动态段路由。**
  详情类信息全部用客户端状态（弹窗/抽屉）展示，不占 URL。
  股票详情使用当前页客户端状态，不新增动态页面路由，绕开 export 的动态路由限制。
- `trailingSlash: true`：导出产物是 `login/index.html` 形态，配合 FastAPI
  `StaticFiles(html=True)` 的目录索引行为，刷新 `/login/` 才不会 404。
- `images: { unoptimized: true }`：export 下 `next/image` 的优化端点不可用。
  本次不画图，但配置要留，否则 build 报 warning/失败。
- 不使用 Server Actions、`cookies()` 服务端读取、Route Handlers（`app/api/**`）
  —— 这些在 export 下全部不可用。全部数据获取走客户端 `fetch`。

### 5.3 未导出路径的 404 与 SPA 回退

`StaticFiles` 不会像 nginx 那样自动回退到 `index.html`。若用户手输 `/signals?date=...` 之类
不存在的路径会得到 404。处理：
- 保持路由集最小（见 5.2），并接受"访问未知路径 = 404"，
  不要为了"看起来像 SPA"去加 catch-all 回退（那会让真 404 与前端 404 混淆）。
- FastAPI 侧要确保**静态挂载不遮蔽 `/api/*` 与 `/health`**：`app.mount("/", ...)` 必须在
  所有 `include_router` **之后**执行。这条顺序写反就是全站 404，T7 有专门测试。

## 6. 前端需要哪些后端接口（依赖核对）

| 前端用途 | 接口 | 来源 |
| --- | --- | --- |
| 登录 | `POST /api/auth/login` | **T7 新增** |
| 判断是否已登录（应用初始化） | `GET /api/auth/me` | **T7 新增** |
| 退出 | `POST /api/auth/logout` | **T7 新增** |
| 股票清单 + 分页总数 | `GET /api/market/stocks?keyword=&limit=&offset=` | 股票列表功能新增；从本地 `stock_basic` 只读查询 |
| 所选股票日线 | `GET /api/market/{symbol}/ohlcv?limit=60\|120\|250\|500` | T5 已有；只读本地行情库 |
| 股票清单未同步提示 | 409 `stock_list_not_seeded` | 股票列表功能新增 |
| 登录、鉴权和登出 | `POST /api/auth/login`、`GET /api/auth/me`、`POST /api/auth/logout` | T7 新增 |

首页保留服务端的选股信号 API 与跑批功能，但浏览器主页面展示股票清单和日 K 线，不展示信号表。

T5 原来只有单日 `date` 参数，看板需要"最近 N 天"区间 → 已在 [T5 §4.3](./tasks/T5-query-apis.md)
补入 `start`/`end`。**这是前端范围追加导致的唯一后端契约变更**，其余接口不动。

## 7. 目录规划（追加部分）

```
Sequoia-X/
├── frontend/                      ★ T8 新增（Next.js 工程，独立 npm 项目）
│   ├── package.json               # next@16.3.x, react, typescript, tailwindcss
│   ├── next.config.ts             # output:'export', trailingSlash:true, images.unoptimized
│   ├── tsconfig.json
│   ├── .gitignore                 # node_modules/ .next/ out/
│   ├── .env.local.example         # 仅 NEXT_PUBLIC_* ；禁止放任何密钥
│   └── src/app/
│       ├── layout.tsx
│       ├── page.tsx               # 股票列表与日 K 线详情
│       ├── login/page.tsx
│       ├── globals.css            # Tailwind 入口
│       └── lib/                   # api.ts（fetch 封装）、types.ts、signals.ts
├── sequoia_x/api/
│   ├── auth.py                    ★ T7 新增：会话表、登录/登出、authenticate()
│   ├── static_app.py 或 app.py    ★ T7：静态挂载
│   └── routes/auth.py             ★ T7 新增
└── scripts/build_frontend.(sh|ps1) ★ T8：next build + 产物校验
```

根 `.gitignore` 追加（T8）：`frontend/node_modules/`、`frontend/.next/`、`frontend/out/`。
**`frontend/out/` 不入库** —— 构建产物进 git 是长期腐化源；部署时由 `scripts/build_frontend.sh`
现做（或 CI 产出）。

## 8. 部署与验证链路（T8 必须跑通）

```bash
# 1. 后端
uv sync --extra dev
uvicorn sequoia_x.api.app:app --port 8000

# 2. 前端构建
cd frontend && npm ci && npm run build          # 产出 frontend/out

# 3. 同源访问
浏览器打开 http://127.0.0.1:8000/
  → 未登录自动跳 /login/
  → 输入 .env 里的 API_KEY
  → 看到股票清单，可搜索并选择股票查看日 K 线
```

开发热更新：`cd frontend && npm run dev`（:3000），
它通过 rewrites 代理 `/api` 到 :8000，所以**两个进程都要起着**。
文档里所有面向用户的说明都要区分"开发（3000）"与"生产（8000）"两个地址，不许混写。

## 9. 对既有约束的影响

- 约束 §10 交付纪律不变：一任务一提交、不 push、不建 PR。
- 约束 §8 编码规范只覆盖 Python。**前端单独一套规范**（T8 §5）：
  函数组件 + hooks、无 `any`、`npm run lint` 与 `npx tsc --noEmit` 必须干净、
  注释与 UI 文案中文、日期一律 `YYYY-MM-DD` 展示。
- 前端不引入新的后端依赖；后端为静态托管只新增 `python-multipart`？—— **不需要**
  （登录请求用 JSON body，不用 form）。T7 若被 FastAPI 要求 `OAuth2PasswordRequestForm`
  才需要它，本方案刻意用 JSON body 避开。
- **单进程假设被前端强化了**：session 存进程内，所以 `uvicorn --workers 2`
  会导致"登录态随机丢失"（两个 worker 各有各的会话表）。
  README 的生产部署章节（T6 §4.4）必须把这条和 baostock 单飞限制并列写清。
