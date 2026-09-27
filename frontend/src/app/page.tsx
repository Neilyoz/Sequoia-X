"use client";
import Link from "next/link";

import { useCallback, useEffect, useRef, useState } from "react";
import KlineChart from "./components/KlineChart";
import Pagination, { PAGE_SIZE_OPTIONS } from "./components/Pagination";
import StateBlock, { type BoardState, type ViewState } from "./components/StateBlock";
import StockList from "./components/StockList";
import StockSearch from "./components/StockSearch";
import { ApiError, api, isRedirectingToLogin } from "./lib/api";
import type { OhlcvItem, OhlcvLimit, StockListItem } from "./lib/types";

const DEFAULT_LIMIT = 50;
const KLINE_LIMITS: OhlcvLimit[] = [60, 120, 250, 500];
const LOADING_LIST: BoardState = { kind: "loading", message: "正在加载股票清单…" };

type KlineState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "empty" }
  | { kind: "error"; message: string };

export default function BoardPage() {
  const [authMode, setAuthMode] = useState<string | null>(null);
  const [authMessage, setAuthMessage] = useState<string | null>(null);
  const [version, setVersion] = useState<string | null>(null);

  const [keyword, setKeyword] = useState("");
  const keywordRef = useRef(keyword);
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [offset, setOffset] = useState(0);
  const [stocks, setStocks] = useState<StockListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [listState, setListState] = useState<ViewState>(LOADING_LIST);

  const [selected, setSelected] = useState<StockListItem | null>(null);
  const [klineLimit, setKlineLimit] = useState<OhlcvLimit>(250);
  const [ohlcv, setOhlcv] = useState<OhlcvItem[]>([]);
  const [klineState, setKlineState] = useState<KlineState>({ kind: "idle" });

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setAuthMode(me.mode);
      })
      .catch((error: unknown) => {
        if (cancelled || isRedirectingToLogin(error)) return;
        setAuthMessage(
          error instanceof ApiError ? error.message : "无法连接服务，请确认后端已启动",
        );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (authMode === null) return;
    let cancelled = false;
    api
      .info()
      .then((info) => {
        if (!cancelled) setVersion(info.version);
      })
      .catch(() => {
        // 服务版本是页脚可选信息，获取失败不影响股票列表。
      });
    return () => {
      cancelled = true;
    };
  }, [authMode]);

  useEffect(() => {
    if (authMode === null) return;
    let cancelled = false;
    api
      .stocks({ keyword, limit, offset })
      .then((data) => {
        if (cancelled) return;
        setStocks(data.items);
        setTotal(data.total);
        setListState(data.items.length === 0 ? { kind: "empty" } : { kind: "ready" });
      })
      .catch((error: unknown) => {
        if (cancelled || isRedirectingToLogin(error)) return;
        setStocks([]);
        setTotal(0);
        if (error instanceof ApiError && error.code === "stock_list_not_seeded") {
          setListState({ kind: "not_seeded" });
        } else {
          setListState({
            kind: "error",
            message: error instanceof ApiError ? error.message : "网络异常，无法获取股票清单",
            hint: "请检查服务状态后重试。",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authMode, keyword, limit, offset]);

  useEffect(() => {
    if (authMode === null || selected === null) return;
    let cancelled = false;
    api
      .ohlcv(selected.symbol, klineLimit)
      .then((data) => {
        if (cancelled) return;
        setOhlcv(data.items);
        setKlineState(data.items.length === 0 ? { kind: "empty" } : { kind: "ready" });
      })
      .catch((error: unknown) => {
        if (cancelled || isRedirectingToLogin(error)) return;
        setOhlcv([]);
        setKlineState({
          kind: "error",
          message: error instanceof ApiError ? error.message : "网络异常，无法获取日线数据",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [authMode, klineLimit, selected]);

  const handleKeywordChange = useCallback((next: string) => {
    if (keywordRef.current === next) return;
    keywordRef.current = next;
    setKeyword(next);
    setOffset(0);
    setListState(LOADING_LIST);
  }, []);

  function changePage(nextOffset: number): void {
    setListState(LOADING_LIST);
    setOffset(nextOffset);
  }

  function changePageSize(nextLimit: number): void {
    setListState(LOADING_LIST);
    setLimit(nextLimit);
    setOffset(0);
  }

  function selectStock(stock: StockListItem): void {
    setSelected(stock);
    setKlineState({ kind: "loading" });
    setOhlcv([]);
  }

  function changeKlineLimit(nextLimit: OhlcvLimit): void {
    setKlineLimit(nextLimit);
    setKlineState({ kind: "loading" });
    setOhlcv([]);
  }

  async function logout(): Promise<void> {
    try {
      await api.logout();
    } catch {
      // 服务端会话可能已失效，用户仍需离开当前页。
    } finally {
      window.location.replace("/login/");
    }
  }

  if (authMessage !== null) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-10">
        <StateBlock state={{ kind: "error", message: authMessage }} />
      </main>
    );
  }

  if (authMode === null) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-10">
        <p className="text-sm text-zinc-500">正在确认登录状态…</p>
        <StateBlock state={{ kind: "loading", message: "正在连接服务…" }} />
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-7xl flex-col px-4 py-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-medium text-zinc-900">Sequoia-X A 股行情</h1>
          <p className="mt-1 text-sm text-zinc-500">搜索本地股票清单，查看对应的日 K 线与成交量。</p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          <Link
            href="/strategy/"
            className="rounded border border-zinc-300 px-3 py-1.5 text-zinc-700 hover:bg-zinc-100"
          >
            策略选股
          </Link>
          {authMode === "open" ? (
            <span className="text-amber-700">未启用鉴权（本地开发模式）</span>
          ) : null}
          {authMode === "session" ? (
            <button
              type="button"
              onClick={() => void logout()}
              className="rounded border border-zinc-300 px-3 py-1.5 text-zinc-700 hover:bg-zinc-100"
            >
              退出登录
            </button>
          ) : null}
        </div>
      </header>

      <div className="mt-6 grid min-w-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(300px,0.85fr)_minmax(0,1.6fr)]">
        <section className="min-w-0 rounded-lg border border-zinc-200 bg-white p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h2 className="font-medium text-zinc-900">股票列表</h2>
            <span className="text-xs text-zinc-500">共 {total} 只</span>
          </div>
          <StockSearch onChange={handleKeywordChange} />
          <div className="mt-3">
            {listState.kind === "ready" ? (
              <StockList items={stocks} selectedSymbol={selected?.symbol ?? null} onSelect={selectStock} />
            ) : (
              <StateBlock state={listState} />
            )}
          </div>
          {listState.kind === "ready" ? (
            <div className="mt-4 border-t border-zinc-100 pt-4">
              <Pagination
                total={total}
                limit={limit}
                offset={offset}
                onPageChange={changePage}
                onLimitChange={changePageSize}
              />
            </div>
          ) : null}
        </section>

        <section className="min-w-0 rounded-lg border border-zinc-200 bg-white p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="font-medium text-zinc-900">日 K 线</h2>
              {selected ? (
                <p className="mt-1 text-sm text-zinc-500">
                  <span className="font-mono">{selected.symbol}</span> · {selected.name}
                </p>
              ) : (
                <p className="mt-1 text-sm text-zinc-500">选择一只股票查看本地日线</p>
              )}
            </div>
            {selected ? (
              <div className="flex items-center gap-1" aria-label="K线显示范围">
                {KLINE_LIMITS.map((count) => (
                  <button
                    key={count}
                    type="button"
                    aria-pressed={klineLimit === count}
                    onClick={() => changeKlineLimit(count)}
                    className={`rounded px-2.5 py-1 text-xs ${
                      klineLimit === count
                        ? "bg-zinc-900 text-white"
                        : "border border-zinc-300 text-zinc-600 hover:bg-zinc-100"
                    }`}
                  >
                    {count} 日
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          <div className="mt-4 min-h-[360px] min-w-0">
            {klineState.kind === "idle" ? (
              <StateBlock
                state={{ kind: "empty", title: "尚未选择股票", detail: "从左侧列表选择一只股票，查看其 K 线和成交量。" }}
              />
            ) : null}
            {klineState.kind === "loading" ? (
              <StateBlock state={{ kind: "loading", message: "正在加载日线数据…" }} />
            ) : null}
            {klineState.kind === "empty" ? (
              <StateBlock
                state={{ kind: "empty", title: "本地暂无日线数据", detail: "请先为该股票回填历史行情后再查看。" }}
              />
            ) : null}
            {klineState.kind === "error" ? (
              <StateBlock state={{ kind: "error", message: klineState.message }} />
            ) : null}
            {klineState.kind === "ready" ? <KlineChart items={ohlcv} /> : null}
          </div>
        </section>
      </div>

      <footer className="pt-5 text-xs text-zinc-400">
        {version ? <span>服务版本 {version}</span> : null}
        <span className="ml-3">列表每页 {PAGE_SIZE_OPTIONS.join(" / ")} 只；行情来自本地数据库。</span>
      </footer>
    </main>
  );
}
