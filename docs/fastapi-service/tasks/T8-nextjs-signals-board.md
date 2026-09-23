# T8 · Next.js 选股信号看板（前端）

**依赖**：T7（登录接口与静态托管就绪）　**阻塞**：T6 收口
**规模**：新建 `frontend/` Next.js 工程 + 1 个构建脚本 + 根 `.gitignore` 追加

## 1. 目标

只有一个页面：**选股信号看板**。能登录、能按日期区间和策略筛选历史选股信号、
能跳到雪球看盘。构建产物由 T7 的挂载点提供，生产只跑一个 uvicorn 进程。

## 2. 动手前先读

- [03-frontend-and-auth.md](../03-frontend-and-auth.md) **全文**，尤其 §5（export 的坑）
- T7 实际代码：`sequoia_x/api/routes/auth.py`（登录契约、错误码）、
  `deps.py` 的 `X-Sequoia-Client` 要求、`app.py` 的 `mount_frontend`
- T5 实际代码：`routes/queries.py` 的 `GET /api/signals` 参数与响应字段
- `docs/fastapi-service/02-architecture.md` §4 接口全表

### 关于"照抄文档"的免责声明（重要）

本文档给出的 Next.js / Tailwind 配置形态基于较早期版本认知编写。
**你面对的是 next@16.3.x，配置项名称、App Router 行为、Tailwind 大版本都可能不同。**
动手前必须做两件事：

1. `npm view next version` / `npm view tailwindcss version` 确认实际装到的版本；
2. 用官方文档核对本文档 §4.1 的每一处配置（尤其 `output: 'export'`、`trailingSlash`、
   `images.unoptimized`、Tailwind 的 PostCSS 插件名与 CSS 入口写法）。

**与文档冲突时以官方文档为准，并把差异写进汇报，同时更新 03 文档**（文档失真比没文档更糟）。
禁止"我记得是这样"。

## 3. 文件清单

新建 `frontend/` 下：
`package.json`、`package-lock.json`（提交）、`next.config.ts`、`tsconfig.json`、
`postcss.config.mjs`（若 Tailwind 版本需要）、`.gitignore`、`.env.local.example`、
`eslint 配置`（`next lint` 集成的形态，按实际版本处理）、
`README.md`（前端自己的启动说明）、
`src/app/layout.tsx`、`src/app/page.tsx`、`src/app/login/page.tsx`、
`src/app/globals.css`、
`src/app/lib/api.ts`、`src/app/lib/types.ts`、`src/app/lib/date.ts`、
`src/app/components/`（`SignalTable.tsx`、`FilterBar.tsx`、`Pagination.tsx`、
`LoginPage.tsx`、`StateBlock.tsx`）

新建根目录：`scripts/build_frontend.sh`、`scripts/build_frontend.ps1`

修改：根 `.gitignore`（追加 `frontend/node_modules/`、`frontend/.next/`、`frontend/out/`）

禁止改动：全部 `sequoia_x/**`、`main.py`、`tests/**`、`data/**`、`docs/fastapi-service/01,02`
（03 可按 §2 的要求更正失真处，改动要在汇报里列出）。

若发现后端接口缺字段/契约不对：**停下来汇报**，不要自己加 `use_effect` 绕过，
也不要顺手改后端（那是 T5/T7 的地盘，改错了会破坏它们的测试）。

## 4. 实现要点

### 4.1 工程配置

```ts
// next.config.ts —— 具体键名以 Next 16.3 官方文档为准
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  // 仅 dev 生效；export 模式会忽略 rewrites（03 §5.1）
  async rewrites() {
    return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" }];
  },
};
```

- Next.js **锁 16.3.x**（`"next": "~16.3.0"`），React 用 Next 16 要求的版本，
  由 `create-next-app` 决定初始版本后再收敛。
- 初始化建议 `npx create-next-app@16.3.6 frontend --typescript --tailwind --eslint --app --src-dir --no-import-alias`
  形态；**跑之前先 `--help` 核对参数是否存在**（不同版本参数集不同）。
- `frontend/.gitignore` 覆盖 `node_modules/`、`.next/`、`out/`、`.env*.local`。
- 遥测：`NEXT_TELEMETRY_DISABLED=1` 写进脚本，并在 README 说明为什么
  （本项目不向 Vercel 上报，属刻意选择）。

### 4.2 API client（`lib/api.ts`）

统一出口，全项目**禁止在组件里直接 `fetch`**：

```ts
const BASE = "";                     // 一律相对路径，见下方红线
export const CLIENT_HEADER = { "X-Sequoia-Client": "web" } as const;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // credentials: "same-origin"
  // 非 2xx → 抛出携带 { status, code } 的 ApiError（code 取 detail.code）
  // 401 且当前不在 /login → location.replace("/login/")
}
export const api = {
  login: (apiKey: string) => ...,
  me: () => ...,
  logout: () => ...,
  strategies: () => ...,
  signals: (q: SignalQuery) => ...,
};
```

- **红线：源码里不得出现 `localhost:8000` / `127.0.0.1:8000` 之类的绝对后端地址。**
  生产同源、开发靠 rewrites，绝对地址会让生产环境立刻坏。
  自查方式：`grep -rn "localhost:\|127.0.0.1:" frontend/src` 只允许出现在注释与 README。
- 只读请求（`GET`）不带 `X-Sequoia-Client` 也行，但**统一带上更省事**；
  写操作必带（T7 会校验）。本页面只有登录是 POST，其余全是 GET。
- 错误码映射成中文文案要集中在这里（`invalid_credentials` → "API Key 不正确"、
  `auth_disabled` → "服务未启用鉴权"、`missing_filter` → "请至少选择一个筛选条件"、
  `database_not_seeded` → 专用空态）。**不要在组件里散落 `if (status === 401)`。**

### 4.3 登录页 `/login/`

- 单输入框（API Key）+ 提交按钮 + 说明文案："密钥配置在服务端 `.env` 的 `API_KEY`"。
- 输入值**只存组件 state**，**绝不写 localStorage / sessionStorage / cookie**（这是本次追加
  决策的核心安全目标，写成注释固化在代码里）。
- 成功后 `location.replace("/")`；失败展示 `invalid_credentials` 文案，
  并处理 429（"尝试次数过多，请稍后再试"）。
- 应用初始化（`layout` 或看板页的 `useEffect`）先调 `/api/auth/me`：
  `mode === "open"` → 直接渲染（本地无鉴权开发）；401 → 跳 `/login/`。
  **加载中要有骨架/占位**，不能先闪一下未登录的空页面再跳（体验差且会误导排查）。

### 4.4 信号看板 `/`（唯一主页面）

筛选区（`FilterBar`）：
- 日期区间：快捷项「近 7 天」「近 30 天」「本月」+ 自定义 `start`/`end`（原生 `<input type="date">`，
  不引日期库）。默认「近 30 天」。
- 策略：下拉多选（选项来自 `GET /api/strategies`，展示 `name` + `webhook_key`；
  接口没给中文名就显示类名，**不要自己造中文名映射**，那是后端事实）。
  若 `/api/strategies` 只返回单值而 `signals` 的 `strategy` 参数只接受单值 →
  退化成单选下拉，**并把这个限制汇报出来**，不要在前端循环并发请求拼"多选"（会打爆后端）。
- 股票代码：文本框，6 位数字，前端做 `^\d{6}$` 校验后再发（后端会 400，但提前拦体验更好）。

表格（`SignalTable`）列：日期、策略、代码、名称、操作（雪球跳转外链）。
- `xueqiu_code` → `https://xueqiu.com/S/SH600519`，`target="_blank" rel="noopener noreferrer"`。
- `name` 为 null 时显示 `—`，不要显示 `undefined` 或空白。
- 同一天多条按日期分组显示（表头行 + 该日条目），这是"看板"比"裸表格"有价值的地方；
  分组只做视觉，不改变分页语义（分页仍按接口返回的 items）。
- 列宽与长列表：`overflow-x-auto` + 最大高度内滚，避免小屏横向挤压。

分页（`Pagination`）：基于 `total/limit/offset`，页码 + 每页条数（50/100/200）。
**切筛选条件必须把 offset 归零**（否则会出现"换了条件却看到空页"的经典 bug）。

状态处理（`StateBlock` 统一）：加载 / 空结果 / 后端错误 / 未回填（409 专用文案 + 指引）。
**四种状态都要有明确中文文案**，不许出现白屏或无限 spinner。

顶部统计条：`total` 条信号、覆盖策略数（前端从当前页 items 去重统计则**必须标注"本页"**，
否则是错误信息 —— 更稳妥的做法是只显总数，别统计假数据）。

其他：退出登录按钮、`/api/info` 里的版本号显示在页脚（有则显，没有不强求）。
**不做自动刷新、不做轮询**（页面是历史数据视图，无实时需求）。

### 4.5 构建脚本 `scripts/build_frontend.(sh|ps1)`

```
1. 检查 node/npm 存在，打印版本
2. cd frontend && npm ci            # 不是 npm install：锁文件不一致要失败而不是静默升级
3. npm run lint 与 npx tsc --noEmit  # 任一失败即退出
4. npm run build
5. 断言 frontend/out/index.html 存在，打印 out/ 文件数与体积
6. 明确提示下一步：uvicorn sequoia_x.api.app:app
```

`npm ci` 失败（锁文件与 `package.json` 不同步）时**不要自动退回 `npm install`**，
把错误抛出来让人处理 —— 静默改锁文件是协作地狱的开始。

### 4.6 视觉

功能优先：Tailwind 默认色板，`zinc` 系为主，选中/告警用 `blue`/`amber`/`red`。
12 列以内布局、`max-w-6xl mx-auto`、字号 14/16px、不写自定义 CSS（`globals.css` 只留 Tailwind 入口）。
不引组件库、不引图标库（必要图标用内联 SVG 或纯文字按钮）。暗色不做。

## 5. 验证要求（无自动化前端测试，靠强制手工清单）

本任务**不引入 Playwright/Jest/Vitest**（超范围）。代价是必须**逐项手工验证并留下证据**：

准备：`uvicorn sequoia_x.api.app:app --port 8000`（配好 `API_KEY`），
库里需要有信号数据 —— 若 `tasks.db` 没有，用一次性脚本往 `signal` 表插
覆盖多日多策略的样例（**插入脚本放 `scripts/`，跑完删除**；
**绝对不许**为了造数据去真跑 `POST /api/tasks/daily`，那会真推飞书并真打 baostock）。

清单（每项写"通过/未通过 + 你观察到什么"）：

开发形态（`npm run dev` :3000）
1. 未登录访问 `/` → 跳 `/login/`，无闪屏
2. 错 key → 中文错误提示；5 次后 → 429 文案
3. 对 key → 进入看板，数据渲染正确
4. Network 面板确认请求走 :3000（rewrites 生效），cookie 被携带
5. 切日期区间/策略/代码 → 列表与 `total` 同步变化，且 offset 归零
6. 翻到第 3 页后改筛选条件 → 回到第 1 页且有数据
7. 6 位代码校验：输入 5 位不发请求
8. 退出登录 → 再访问 `/` 回登录页

生产形态（`npm run build` → 访问 :8000）
9. `http://127.0.0.1:8000/` 打开即登录页，登录后看板正常
10. 刷新 `/login/` 与 `/` 都不 404（`trailingSlash` + `html=True` 组合成立）
11. `http://127.0.0.1:8000/health` 与 `/docs` 未被静态挂载遮蔽
12. 浏览器 Network 无任何指向 :3000 的请求（生产不依赖 dev server）
13. 空库/未回填 → 409 专用文案
14. 移动端宽度（DevTools 375px）表格可横滚不破版

**12 是最有价值的一条**（说明绝对路径红线守住了）；**10/11 是 03 §5 那两条坑的实测**。

## 6. 验收标准

```bash
cd frontend && npm ci && npm run lint && npx tsc --noEmit && npm run build
bash scripts/build_frontend.sh        # 或 powershell scripts/build_frontend.ps1
grep -rn "localhost:\|127.0.0.1:" frontend/src   # 只允许注释/README
git status --ignored frontend | head -20          # node_modules/.next/out 已被忽略
pytest -q                            # 后端测试不能被前端任务改坏（应该完全没动后端）
```

逐条自查：

1. `frontend/out/` **未被 git 跟踪**（`git ls-files frontend/out` 为空）。
2. `frontend/package-lock.json` **已提交**（否则 `npm ci` 别人跑不了）。
3. 源码零 `any`、零 `@ts-ignore`、零 `eslint-disable`（有则逐条解释为什么必要）。
4. 组件里没有直接 `fetch`，全部经 `lib/api.ts`。
5. 四个状态（加载/空/错误/未回填）在代码里都能找到对应分支，不是"应该不会出现"。
6. 没有新增动态段路由（`src/app/**/[xxx]/page.tsx` 不存在）。
7. `git diff sequoia_x/` 为空 —— 前端任务不动后端。
8. §5 的 14 项清单逐项有结论。

## 7. 完成后汇报

- 实际装到的 next / react / tailwindcss 版本，以及**本文档 §4.1 与官方文档不符之处**（逐条）
- `create-next-app` 用的完整命令行与生成的文件集
- `trailingSlash` 与 `StaticFiles(html=True)` 的实测组合行为 → 03 §5.2 是否需要修订
- `/api/strategies` 与 `/api/signals` 契约是否够用（尤其策略多选与统计数），缺什么
- 14 项手工清单的逐项结论
- 截图（看板 + 登录页 + 空态各一张）放到 `docs/fastapi-service/screenshots/`
  并在 README 引用；不方便截图就用文字描述实际渲染结果，**不要伪造**
