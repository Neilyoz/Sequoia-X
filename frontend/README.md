# Sequoia-X 前端（信号看板）

Next.js 16 静态导出工程，只有两个路由：`/login/`（登录）与 `/`（信号看板）。
构建产物在 `out/`，由后端 `sequoia_x.api.app` 的 `mount_frontend` 挂载，
生产形态只跑一个 uvicorn 进程（03-frontend-and-auth.md）。

## 环境要求

- Node.js 24（本机实测 v24.18/v24.19），npm 随 Node 一起装。
- 包管理器**只用 npm**（任务书决策）：`package-lock.json` 提交入库，
  安装一律 `npm ci`，不要用 pnpm/yarn 生成第二份锁文件。

## 开发模式

需要后端先在 :8000 跑起来（开发期不配 `API_KEY` 即为无鉴权模式，登录页会自动跳过）：

```bash
# 终端 1（仓库根目录）：API 服务
API_KEY= uv run uvicorn sequoia_x.api.app:app --port 8000

# 终端 2：前端 dev server（:3000，/api/* 由 next.config.ts 的 rewrites 转给 :8000）
cd frontend && npm run dev
```

rewrites 只在 `npm run dev` 下生效；`next build`（静态导出）会忽略它们——
这正是"源码只写相对路径"能同时成立两种形态的原因。

## 构建与检查

日常验收四条命令（与任务书 §6 一致）：

```bash
cd frontend
npm ci
npm run lint
npx tsc --noEmit
npm run build        # 产物落在 out/（git 已忽略）
```

或者一步到位（含工具链检查与产物校验）：

```bash
bash scripts/build_frontend.sh          # Git Bash / Linux / macOS
powershell scripts/build_frontend.ps1   # Windows PowerShell
```

## 遥测说明

构建脚本固定导出 `NEXT_TELEMETRY_DISABLED=1`：本项目不向 Vercel 上报使用数据，
这是刻意选择（服务部署在内网本机，上报没有收益）。手写 `npx next build` 时
请自行带上该变量。

## 目录约定

- `src/app/lib/api.ts` 是**全站唯一**的 fetch 出口：组件里不允许直接 `fetch`，
  错误码→中文文案、401→跳登录都在这里（见文件头注释）。
- 源码禁止出现绝对后端地址（`localhost:8000` 等），唯一的例外是
  `next.config.ts` 里 dev-only 的 rewrites 目标。自查：
  `grep -rn "localhost:\|127.0.0.1:" src` 只允许命中注释与该文件。
- `trailingSlash: true` + 静态导出：产物是 `index.html` / `login/index.html` 目录形态，
  与后端 `StaticFiles(html=True)` 的解析规则配套，两者不能只改一边。
- 不引组件库、图标库、日期库；`globals.css` 只放 Tailwind 入口一行。

## 登录与安全边界

- API Key 输入框的值只存组件 state，**不写 localStorage / sessionStorage / cookie**；
  登录成功后浏览器只持有后端下发的 HttpOnly `sequoia_session` cookie（默认 7 天）。
- 生产如要挂到 https 之外，需同步考虑 cookie 的 `secure` 标记（当前刻意未设，
  因为默认监听 127.0.0.1 的 http，详见 `sequoia_x/api/routes/auth.py` 注释）。
