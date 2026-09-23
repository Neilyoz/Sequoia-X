import type { NextConfig } from "next";

/**
 * 前端工程配置（T8 · 任务书 §4.1，键名已按 Next 16.3 官方文档核对）。
 *
 * - `output: "export"`：`next build` 产出纯静态目录 `out/`，由 FastAPI
 *   `StaticFiles(html=True)` 挂载在 `/`（`sequoia_x/api/app.py:mount_frontend`）。
 * - `trailingSlash: true`：导出形态是 `login/index.html` 而非 `login.html`，
 *   配合 StaticFiles 的目录索引行为，刷新 `/login/` 才不会 404（03 §5.2）。
 * - `images.unoptimized: true`：静态导出下 `next/image` 的默认优化端点不可用。
 *   本页不画图，但配置必须留，否则 build 直接报错。
 * - `rewrites`：官方文档明确列在 static export 的**不支持特性**里。
 *   实测结论（见 frontend/README.md「开发/生产两套形态」）：`next dev` 仍然执行
 *   rewrites，`next build` 阶段会忽略它 —— 这正是 03 §5.1 描述的形态差异，
 *   所以这里无条件声明，不用 phase 分支包一层（少一层心智负担）。
 */
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  // 仅 `next dev`（:3000）生效：把 /api/* 转给本机 uvicorn（:8000），避免跨源与 CORS。
  // 生产是同源部署（浏览器只访问 :8000），根本不经过这里。
  // 目标地址只允许出现在这一处配置里：业务源码一律写相对路径 /api/...（任务书 §4.2 红线）。
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
