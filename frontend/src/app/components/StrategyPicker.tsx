"use client";
import Link from "next/link";

import { useEffect, useRef, useState } from "react";
import { ApiError, api, isRedirectingToLogin } from "../lib/api";
import type { SignalItem, StrategyInfo, TaskResponse } from "../lib/types";

const POLL_INTERVAL_MS = 2000;

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function isActive(status: TaskResponse["status"]): boolean {
  return status === "pending" || status === "running";
}

export default function StrategyPicker() {
  const [authMode, setAuthMode] = useState<string | null>(null);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [selected, setSelected] = useState("");
  const [loadingStrategies, setLoadingStrategies] = useState(true);
  const [strategyError, setStrategyError] = useState<string | null>(null);
  const [task, setTask] = useState<TaskResponse | null>(null);
  const [items, setItems] = useState<SignalItem[]>([]);
  const [total, setTotal] = useState(0);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestVersion = useRef(0);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setAuthMode(me.mode);
      })
      .catch((cause: unknown) => {
        if (cancelled || isRedirectingToLogin(cause)) return;
        setStrategyError(
          cause instanceof ApiError ? cause.message : "无法连接服务，请确认后端已启动",
        );
        setLoadingStrategies(false);
      });
    return () => {
      cancelled = true;
      requestVersion.current += 1;
    };
  }, []);

  useEffect(() => {
    if (authMode === null) return;
    let cancelled = false;
    setLoadingStrategies(true);
    api
      .strategies()
      .then((data) => {
        if (cancelled) return;
        setStrategies(data);
        setSelected(data[0]?.name ?? "");
        setStrategyError(null);
      })
      .catch((cause: unknown) => {
        if (cancelled || isRedirectingToLogin(cause)) return;
        setStrategyError(
          cause instanceof ApiError ? cause.message : "网络异常，无法获取策略列表",
        );
      })
      .finally(() => {
        if (!cancelled) setLoadingStrategies(false);
      });
    return () => {
      cancelled = true;
    };
  }, [authMode]);

  async function runStrategy(): Promise<void> {
    if (!selected || running) return;
    const version = ++requestVersion.current;
    setRunning(true);
    setError(null);
    setTask(null);
    setItems([]);
    setTotal(0);

    try {
      let current = await api.submitDaily([selected]);
      if (version !== requestVersion.current) return;
      setTask(current);

      while (isActive(current.status)) {
        await wait(POLL_INTERVAL_MS);
        if (version !== requestVersion.current) return;
        current = await api.task(current.task_id);
        if (version !== requestVersion.current) return;
        setTask(current);
      }

      if (current.status === "failed") {
        setError(current.error || "策略运行失败，请查看任务日志后重试");
        return;
      }

      const result = await api.taskSignals(current.task_id);
      if (version !== requestVersion.current) return;
      setItems(result.items);
      setTotal(result.total);
    } catch (cause: unknown) {
      if (version !== requestVersion.current || isRedirectingToLogin(cause)) return;
      if (cause instanceof ApiError && cause.code === "task_already_running") {
        setError("当前已有跑批任务正在执行。请等待任务结束后再运行选股。");
      } else {
        setError(cause instanceof ApiError ? cause.message : "运行选股时发生网络错误，请重试");
      }
    } finally {
      if (version === requestVersion.current) setRunning(false);
    }
  }

  async function logout(): Promise<void> {
    try {
      await api.logout();
    } catch {
      // 即使会话已失效，也离开当前页。
    } finally {
      window.location.replace("/login/");
    }
  }

  const selectedStrategy = strategies.find((strategy) => strategy.name === selected);

  if (authMode === null && strategyError === null) {
    return (
      <main className="mx-auto min-h-screen max-w-5xl px-4 py-10">
        <p className="text-sm text-zinc-500">正在确认登录状态…</p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-5xl flex-col px-4 py-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-medium text-zinc-900">策略选股</h1>
          <p className="mt-1 text-sm text-zinc-500">运行指定策略，并查看本次运行筛选出的股票。</p>
        </div>
        <nav className="flex items-center gap-3 text-sm">
          <Link className="text-zinc-600 hover:text-zinc-900" href="/">股票行情</Link>
          {authMode === "open" ? <span className="text-amber-700">本地开发模式</span> : null}
          {authMode === "session" ? (
            <button
              type="button"
              onClick={() => void logout()}
              className="rounded border border-zinc-300 px-3 py-1.5 text-zinc-700 hover:bg-zinc-100"
            >
              退出登录
            </button>
          ) : null}
        </nav>
      </header>

      <section className="mt-6 rounded-lg border border-zinc-200 bg-white p-5">
        <label htmlFor="strategy-select" className="block text-sm font-medium text-zinc-800">
          选择策略
        </label>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <select
            id="strategy-select"
            value={selected}
            disabled={loadingStrategies || running || strategies.length === 0}
            onChange={(event) => {
              setSelected(event.target.value);
              setTask(null);
              setItems([]);
              setTotal(0);
              setError(null);
            }}
            className="min-w-64 rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-800 disabled:bg-zinc-100"
          >
            {strategies.length === 0 ? <option value="">暂无可用策略</option> : null}
            {strategies.map((strategy) => (
              <option key={strategy.name} value={strategy.name}>
                {strategy.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void runStrategy()}
            disabled={loadingStrategies || running || !selected}
            className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:cursor-not-allowed disabled:bg-zinc-400"
          >
            {running ? "正在选股…" : "开始选股"}
          </button>
        </div>
        <p className="mt-3 text-xs leading-5 text-zinc-500">
          每次运行会先同步最新行情，再执行所选策略；结果只显示本次任务，不会推送飞书。
        </p>
        {strategyError ? <p className="mt-3 text-sm text-rose-700">{strategyError}</p> : null}
      </section>

      <section className="mt-5 flex-1 rounded-lg border border-zinc-200 bg-white p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-medium text-zinc-900">选股结果</h2>
            <p className="mt-1 text-sm text-zinc-500">
              {task
                ? `任务 ${task.task_id.slice(0, 8)} · 策略 ${selectedStrategy?.name ?? selected}`
                : "选择策略并开始运行后，股票结果会显示在这里。"}
            </p>
          </div>
          {task?.status === "success" && !running && error === null ? (
            <span className="text-sm text-zinc-500">共 {total} 只</span>
          ) : null}
        </div>

        {running ? (
          <div className="mt-5 rounded-md bg-blue-50 px-4 py-3 text-sm text-blue-800" role="status">
            {task?.status === "pending"
              ? "任务已排队，等待开始…"
              : "正在同步行情并运行策略，请稍候…"}
          </div>
        ) : null}
        {error ? (
          <div className="mt-5 rounded-md bg-rose-50 px-4 py-3 text-sm text-rose-800" role="alert">
            {error}
          </div>
        ) : null}
        {task?.status === "success" && !running && !error ? (
          total === 0 ? (
            <div className="mt-5 rounded-md bg-zinc-50 px-4 py-8 text-center text-sm text-zinc-500">
              本次运行没有筛选出符合条件的股票。
            </div>
          ) : (
            <div className="mt-5 overflow-x-auto">
              <table className="w-full min-w-[560px] border-collapse text-left text-sm">
                <thead>
                  <tr className="border-b border-zinc-200 text-xs text-zinc-500">
                    <th className="px-3 py-2 font-medium">序号</th>
                    <th className="px-3 py-2 font-medium">股票代码</th>
                    <th className="px-3 py-2 font-medium">股票名称</th>
                    <th className="px-3 py-2 font-medium">交易日期</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, index) => (
                    <tr
                      key={`${item.symbol}-${item.trade_date}-${index}`}
                      className="border-b border-zinc-100 last:border-0"
                    >
                      <td className="px-3 py-3 text-zinc-500">{index + 1}</td>
                      <td className="px-3 py-3 font-mono text-zinc-800">{item.symbol}</td>
                      <td className="px-3 py-3 text-zinc-800">{item.name ?? "—"}</td>
                      <td className="px-3 py-3 text-zinc-600">{item.trade_date}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        ) : null}
        {task?.status === "failed" && !error ? (
          <div className="mt-5 rounded-md bg-rose-50 px-4 py-3 text-sm text-rose-800" role="alert">
            {task.error || "策略运行失败，请重试。"}
          </div>
        ) : null}
      </section>
    </main>
  );
}
