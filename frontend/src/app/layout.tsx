import type { ReactNode } from "react";
import "./globals.css";

/**
 * 根布局：只做三件事 —— 中文 lang、Tailwind 入口样式、页面标题。
 *
 * 刻意不用 `next/font/google`：模板自带的 Geist 字体在 build 时要联网下载，
 * 而本项目的生产形态是内网单进程服务，字体拉不到就会拖慢甚至打断构建。
 * 排版直接用 Tailwind 默认字体栈（§4.6「不写自定义 CSS」）。
 */
export const metadata = {
  title: "Sequoia-X A 股行情",
  description: "浏览本地 A 股清单并查看股票日 K 线与成交量。",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="bg-zinc-50 text-zinc-900 antialiased">{children}</body>
    </html>
  );
}
