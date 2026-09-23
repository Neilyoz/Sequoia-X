#!/usr/bin/env bash
# 前端构建脚本（任务书 §4.5）：Git Bash / Linux / macOS 均可用。
#
# 为什么 NEXT_TELEMETRY_DISABLED=1：本项目不向 Vercel 上报使用数据（刻意选择，
# 详见 frontend/README.md），写在脚本里而不是 .env.local，是为了"别人直接跑
# npx next build"时也不会漏掉——脚本是唯一的正式入口。
#
# 任一步失败即退出（set -euo pipefail）：半成品 out/ 挂载进生产比构建失败更难排查。
set -euo pipefail

export NEXT_TELEMETRY_DISABLED=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"

echo "== 1/5 工具链检查 =="
command -v node >/dev/null 2>&1 || { echo "缺少 node：请安装 Node 24（本项目实测 v24.19）"; exit 1; }
command -v npm  >/dev/null 2>&1 || { echo "缺少 npm：请随 Node 一起安装（本项目实测 npm 12）"; exit 1; }
echo "node: $(node --version)"
echo "npm : $(npm --version)"

cd "${FRONTEND_DIR}"

echo "== 2/5 安装依赖（npm ci）=="
# 刻意用 npm ci 而不是 npm install：锁文件与 package.json 不一致时必须失败，
# 而不是静默改写 package-lock.json（静默改锁文件是协作地狱的开始，任务书 §4.5）。
# 本机 npm 12 会对未列入 allowScripts 的第三方 install script 给一条 WARNING
# （实测只有 unrs-resolver 的 postinstall 被拦，lint/build 均不受影响）；
# 这里不自动 `npm install-scripts approve`，是否放行第三方脚本应由人决定。
npm ci

echo "== 3/5 代码检查（eslint + tsc）=="
npm run lint
npx tsc --noEmit

echo "== 4/5 静态导出（next build）=="
npm run build

echo "== 5/5 产物校验 =="
INDEX_HTML="${FRONTEND_DIR}/out/index.html"
if [[ ! -f "${INDEX_HTML}" ]]; then
  echo "构建未产出 out/index.html，挂载点会退化（app.py:mount_frontend 只 WARNING）"
  exit 1
fi
FILE_COUNT="$(find "${FRONTEND_DIR}/out" -type f | wc -l | tr -d ' ')"
SIZE="$(du -sh "${FRONTEND_DIR}/out" | cut -f1)"
echo "产物：frontend/out —— ${FILE_COUNT} 个文件，合计 ${SIZE}"
echo "out/login/index.html 存在：$([[ -f "${FRONTEND_DIR}/out/login/index.html" ]] && echo 是 || echo 否)"

echo
echo "下一步（生产形态，根目录执行）："
echo "  uv run uvicorn sequoia_x.api.app:app --port 8000"
echo "然后浏览器访问 http://127.0.0.1:8000/"
