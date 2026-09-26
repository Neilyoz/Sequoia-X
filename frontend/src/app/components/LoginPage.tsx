/**
 * 登录页（任务书 §4.3）。
 *
 * 安全约定（本次追加决策的核心，改动前请先读完）：
 * - API Key **只存在于组件 state**，随页面卸载消失。绝不写入浏览器持久化存储或普通 cookie：
 *   那等于把服务端唯一凭据长期留在浏览器里，跨站脚本一偷一个准，
 *   也让"HttpOnly session cookie"这层设计白做（03 §2）。
 * - 因此**也不使用受控之外的任何缓存**：不自动填充、不记住上次输入。
 * - 成功用 `location.replace`：不留历史记录，用户按"后退"不会退回登录页。
 */

"use client";

import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api } from "../lib/api";

export default function LoginPage() {
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 服务端未配 API_KEY 时登录接口必然 400 auth_disabled，
  // 停在登录页只会让人以为"key 打错了"，所以探到 open 就直接回看板。
  const [openMode, setOpenMode] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (cancelled) {
          return;
        }
        if (me.mode === "open") {
          setOpenMode(true);
        } else {
          // 已登录（session 还没过期）就别再拦在门口。
          window.location.replace("/");
        }
      })
      .catch(() => {
        // 未登录时 /api/auth/me 返回 401，是唯一预期内的失败：留在本页输 key 即可。
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const trimmed = apiKey.trim();
    if (trimmed === "") {
      setError("请输入 API Key");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await api.login(trimmed);
      window.location.replace("/");
    } catch (caught) {
      // 401 invalid_credentials / 429 login_rate_limited / 400 auth_disabled 的中文文案
      // 全部由 lib/api.ts 的错误码表给出，这里只兜底非 ApiError 的网络异常。
      setError(caught instanceof ApiError ? caught.message : "无法连接服务，请确认后端已启动");
      setSubmitting(false);
    }
  }

  if (openMode) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-zinc-50 px-4">
        <div className="w-full max-w-sm rounded border border-amber-200 bg-amber-50 p-6 text-sm text-amber-900">
          <p className="text-base font-medium">服务未启用鉴权</p>
          <p className="mt-2">
            后端没有配置 <code className="rounded bg-amber-100 px-1">API_KEY</code>
            ，接口处于无鉴权模式，无需登录。正在返回看板…
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-zinc-50 px-4">
      <div className="w-full max-w-sm rounded border border-zinc-200 bg-white p-6">
        <h1 className="text-lg font-medium text-zinc-900">登录 Sequoia-X 行情页</h1>
        <p className="mt-1 text-sm text-zinc-500">
          密钥配置在服务端 <code className="rounded bg-zinc-100 px-1">.env</code> 的{" "}
          <code className="rounded bg-zinc-100 px-1">API_KEY</code>。
        </p>

        <form onSubmit={handleSubmit} className="mt-5 space-y-3">
          <label className="block text-sm text-zinc-700">
            <span className="mb-1 block">API Key</span>
            {/* type=password + 不持久化：key 只在内存里存在到本次提交为止。 */}
            <input
              type="password"
              value={apiKey}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setApiKey(event.target.value)}
              className="block w-full rounded border border-zinc-300 px-3 py-2 text-sm text-zinc-900"
            />
          </label>

          {error ? <p className="text-sm text-red-700">{error}</p> : null}

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-blue-300"
          >
            {submitting ? "登录中…" : "登录"}
          </button>
        </form>

        <p className="mt-4 text-xs text-zinc-400">
          登录成功后浏览器只持有一个 HttpOnly 会话 cookie（有效期 7 天），密钥本身不会被存储。
        </p>
      </div>
    </main>
  );
}
