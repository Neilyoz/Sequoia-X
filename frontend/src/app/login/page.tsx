import LoginPage from "../components/LoginPage";

/**
 * `/login/` 路由壳（任务书 §3）。
 *
 * 静态导出 + `trailingSlash: true` 下产物是 `login/index.html`，
 * 所以这里只有一层转发，逻辑全在 `components/LoginPage.tsx`（它是 client 组件）。
 * 本文件保持 server 组件：不需要任何客户端能力，少一个打包进浏览器的模块。
 */
export default function LoginPageRoute() {
  return <LoginPage />;
}
