/**
 * 选股信号看板（唯一主页面，任务书 §4.4）。
 *
 * 组件树：本页只管"状态装配 + 数据获取"，展示全部交给子组件。
 *
 * 两条容易写错的纪律，留在这里当注释：
 * 1. **切筛选条件必须把 offset 归零**（清单第 5/6 项）。所有条件变更都走 `applyFilters`，
 *    不给子组件直接改 filters 的旁路，这样"归零"这件事只有一处实现。
 * 2. **鉴权未确认前不渲染空看板**（§4.3）。首屏渲染的是占位，`/api/auth/me` 回来才决定
 *    渲染内容或跳登录页，避免"闪一下未登录空页面再跳"。
 */

"use client";

import { useCallback, useEffect, useState } from "react";
import FilterBar, { defaultFilters, type Filters } from "./components/FilterBar";
import Pagination, { PAGE_SIZE_OPTIONS } from "./components/Pagination";
import SignalTable from "./components/SignalTable";
import StateBlock, { type BoardState, type ViewState } from "./components/StateBlock";
import { ApiError, NOT_SEEDED_CODE, api, isRedirectingToLogin } from "./lib/api";
import type { SignalItem, StrategyInfo } from "./lib/types";

/** 默认每页条数：与后端 `limit` 默认值一致，也在 50/100/200 三档里。 */
const DEFAULT_LIMIT = 100;

const READY_VIEW: ViewState = { kind: "ready" };
// 加载态常量刻意标成 BoardState（而非 ViewState）：它要直接喂给 StateBlock。
const LOADING_VIEW: BoardState = { kind: "loading" };

export default function BoardPage() {
  // 鉴权态：unknown=还没问过 /me（渲染占位），open/session/apikey=可以继续。
  const [authMode, setAuthMode] = useState<string | null>(null);
  const [authMessage, setAuthMessage] = useState<string | null>(null);

  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [version, setVersion] = useState<string | null>(null);

  const [filters, setFilters] = useState<Filters>(defaultFilters);
  const [limit, setLimit] = useState<number>(DEFAULT_LIMIT);
  const [offset, setOffset] = useState<number>(0);

  const [items, setItems] = useState<SignalItem[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [view, setView] = useState<ViewState>(LOADING_VIEW);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) {
          setAuthMode(me.mode);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        if (!isRedirectingToLogin(error)) {
          // 非"跳登录中"的失败（服务没起、500）要如实报出来，不能卡在占位上。
          setAuthMessage(
            error instanceof ApiError ? error.message : "无法连接服务，请确认后端已启动",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 策略下拉与服务版本：都属于"辅助信息"，失败不影响主列表，静默降级即可。
  // 必须等 /api/auth/me 有结果再发：未登录时这两个接口同样回 401，抢先发会把
  // "一次 401 跳登录"变成三次 401 + 三次 location.replace（Network 面板可直接观察到）。
  useEffect(() => {
    if (authMode === null) {
      return;
    }
    let cancelled = false;
    api
      .strategies()
      .then((list) => {
        if (!cancelled) {
          setStrategies(list);
        }
      })
      .catch(() => {
        // 拿不到策略清单时下拉只剩"全部策略"：日期/代码筛选仍可用，不值得为它整页报错。
      });
    api
      .info()
      .then((info) => {
        if (!cancelled) {
          setVersion(info.version);
        }
      })
      .catch(() => {
        // 版本号是页脚的可选信息（任务书 §4.4「有则显，没有不强求」）。
      });
    return () => {
      cancelled = true;
    };
  }, [authMode]);

  useEffect(() => {
    if (authMode === null) {
      return;
    }
    let cancelled = false;
    // 注意：这里**不**在 effect 体内 setView(loading)。加载态由"用户改了条件/翻了页"
    // 那三个事件处理器置位（见 applyFilters / applyLimit / applyOffset），effect 只在
    // 回调里落地结果 —— 既符合 react-hooks/set-state-in-effect 的要求，也避免
    // 首次挂载时"先 loading 再 loading"的多余一次渲染。
    api
      .signals({
        start: filters.start,
        end: filters.end,
        strategy: filters.strategy,
        symbol: filters.symbol,
        limit,
        offset,
      })
      .then((data) => {
        if (cancelled) {
          return;
        }
        setItems(data.items);
        setTotal(data.total);
        setView(data.items.length === 0 ? { kind: "empty" } : READY_VIEW);
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setItems([]);
        setTotal(0);
        if (error instanceof ApiError && error.code === NOT_SEEDED_CODE) {
          setView({ kind: "not_seeded" });
        } else if (!isRedirectingToLogin(error)) {
          setView({
            kind: "error",
            message: error instanceof ApiError ? error.message : "网络异常，无法获取信号",
            hint: "筛选条件已保留，修正后可直接重新查询",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authMode, filters, limit, offset]);

  /** 条件变更的唯一入口：置加载态 + offset 归零（清单第 5/6 项）。 */
  const applyFilters = useCallback((next: Filters) => {
    setView(LOADING_VIEW);
    setFilters(next);
    setOffset(0);
  }, []);

  const applyLimit = useCallback((next: number) => {
    setView(LOADING_VIEW);
    setLimit(next);
    setOffset(0);
  }, []);

  const applyOffset = useCallback((next: number) => {
    setView(LOADING_VIEW);
    setOffset(next);
  }, []);

  async function logout(): Promise<void> {
    try {
      await api.logout();
    } catch {
      // 登出失败也要离开本页：服务端会话要么已失效，要么 7 天后自己失效，
      // 停在"点了没反应"的看板比多一个错误提示更糟。
    } finally {
      window.location.replace("/login/");
    }
  }

  if (authMessage !== null) {
    return (
      <main className="mx-auto max-w-6xl px-4 py-10">
        <StateBlock state={{ kind: "error", message: authMessage }} />
      </main>
    );
  }

  if (authMode === null) {
    return (
      <main className="mx-auto max-w-6xl px-4 py-10">
        <p className="text-sm text-zinc-500">正在确认登录状态…</p>
        <div className="mt-3">
          <StateBlock state={LOADING_VIEW} />
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col px-4 py-6">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-medium text-zinc-900">Sequoia-X 选股信号看板</h1>
          <p className="mt-1 text-sm text-zinc-500">
            历史选股结果与雪球看盘入口。数据由服务端跑批任务维护，本页只读、不自动刷新。
          </p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          {authMode === "open" ? (
            <span className="text-amber-700">未启用鉴权（本地开发模式）</span>
          ) : null}
          {authMode === "session" ? (
            <button
              type="button"
              onClick={() => {
                void logout();
              }}
              className="rounded border border-zinc-300 px-3 py-1.5 text-zinc-700 hover:bg-zinc-100"
            >
              退出登录
            </button>
          ) : null}
        </div>
      </header>

      <div className="mt-5 space-y-4">
        <FilterBar strategies={strategies} value={filters} onChange={applyFilters} />

        {/* 顶部统计条：只报接口给的 total，不统计"本页去重策略数"那种会误导人的假数据。 */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-zinc-600">
          <span>
            共 <strong className="font-medium text-zinc-900">{total}</strong> 条信号
          </span>
          <span className="text-zinc-400">
            {filters.start} 至 {filters.end}
          </span>
          {filters.strategy ? (
            <span className="text-zinc-400">策略 {filters.strategy}</span>
          ) : null}
          {filters.symbol ? (
            <span className="text-zinc-400">代码 {filters.symbol}</span>
          ) : null}
        </div>

        {view.kind === "ready" ? <SignalTable items={items} /> : <StateBlock state={view} />}

        {view.kind === "ready" && (
          <Pagination
            total={total}
            limit={limit}
            offset={offset}
            onPageChange={applyOffset}
            onLimitChange={applyLimit}
          />
        )}
      </div>

      <footer className="mt-auto pt-8 text-xs text-zinc-400">
        {version ? <span>服务版本 {version}</span> : null}
        <span className="ml-3">
          每页可选 {PAGE_SIZE_OPTIONS.join(" / ")} 条；信号明细以服务端跑批结果为准。
        </span>
      </footer>
    </main>
  );
}
