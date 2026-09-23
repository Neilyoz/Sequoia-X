/**
 * 筛选区（任务书 §4.4）：日期区间（快捷项 + 自定义）、策略、股票代码。
 *
 * 三条实现约定：
 * - 不引日期库，用原生 `<input type="date">`；值本身就是 `YYYY-MM-DD`，与后端契约同形，
 *   所以中间不需要任何格式转换层（少一层就少一处 bug）。
 * - 不造中文名映射：策略展示后端给的类名 + `webhook_key`，中文名是后端事实，前端不编。
 * - 代码框只接受 6 位数字才提交（后端也会 400，但提前拦体验更好，见清单第 7 项）。
 */

"use client";

import { useState } from "react";
import { lastNDays, thisMonth, type DateRange } from "../lib/date";
import type { StrategyInfo } from "../lib/types";

/** 日期快捷键；`custom` 表示用户手改过任一端点。 */
export type DatePreset = "last7" | "last30" | "thisMonth" | "custom";

/**
 * 看板筛选条件。`strategy`/`symbol` 的空串统一表示"不过滤"，
 * 这样"清空"与"未填"是同一个状态，不会长出第三种语义。
 */
export type Filters = {
  preset: DatePreset;
  start: string;
  end: string;
  strategy: string;
  symbol: string;
};

const SYMBOL_PATTERN = /^\d{6}$/;

const PRESETS: { key: Exclude<DatePreset, "custom">; label: string; range: () => DateRange }[] = [
  { key: "last7", label: "近 7 天", range: () => lastNDays(7) },
  { key: "last30", label: "近 30 天", range: () => lastNDays(30) },
  { key: "thisMonth", label: "本月", range: () => thisMonth() },
];

/** 默认「近 30 天」（任务书 §4.4）。 */
export function defaultFilters(): Filters {
  const range = lastNDays(30);
  return { preset: "last30", start: range.start, end: range.end, strategy: "", symbol: "" };
}

type Props = {
  strategies: StrategyInfo[];
  value: Filters;
  /** 任何条件变更都会经这里；分页 offset 归零由父组件负责（见 page.tsx 的 applyFilters）。 */
  onChange: (next: Filters) => void;
};

export default function FilterBar({ strategies, value, onChange }: Props) {
  // 代码框的中间态（可能只有 3 位）只活在组件里：不合法就不往 Filters 里写，
  // 于是父组件的 effect 永远不会用半个代码去发请求。
  const [symbolDraft, setSymbolDraft] = useState(value.symbol);
  const symbolRejected = symbolDraft !== "" && !SYMBOL_PATTERN.test(symbolDraft);
  const rangeInverted = value.start !== "" && value.end !== "" && value.start > value.end;

  function handleSymbolChange(next: string): void {
    setSymbolDraft(next);
    if (next === "" || SYMBOL_PATTERN.test(next)) {
      onChange({ ...value, symbol: next });
    }
  }

  function handlePreset(key: Exclude<DatePreset, "custom">, range: DateRange): void {
    onChange({ ...value, preset: key, start: range.start, end: range.end });
  }

  function handleEndpointChange(endpoint: "start" | "end", next: string): void {
    // 手改任一端点即退出快捷态：不然"近 30 天"亮着、区间却不是 30 天，是最容易骗到排查者的界面。
    onChange({ ...value, preset: "custom", [endpoint]: next });
  }

  return (
    <section className="rounded border border-zinc-200 bg-white p-4">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
        <div className="flex items-end gap-2">
          <span className="mb-2 text-sm text-zinc-500">日期区间</span>
          {PRESETS.map((preset) => (
            <button
              key={preset.key}
              type="button"
              onClick={() => handlePreset(preset.key, preset.range())}
              className={
                value.preset === preset.key
                  ? "rounded bg-blue-600 px-3 py-1.5 text-sm text-white"
                  : "rounded border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-100"
              }
            >
              {preset.label}
            </button>
          ))}
        </div>

        <div className="flex items-end gap-2">
          <label className="text-sm text-zinc-500">
            <span className="mb-1 block">开始</span>
            <input
              type="date"
              value={value.start}
              onChange={(event) => handleEndpointChange("start", event.target.value)}
              className="rounded border border-zinc-300 px-2 py-1.5 text-sm text-zinc-800"
            />
          </label>
          <label className="text-sm text-zinc-500">
            <span className="mb-1 block">结束</span>
            <input
              type="date"
              value={value.end}
              onChange={(event) => handleEndpointChange("end", event.target.value)}
              className="rounded border border-zinc-300 px-2 py-1.5 text-sm text-zinc-800"
            />
          </label>
          {rangeInverted ? (
            <p className="mb-2 text-sm text-amber-700">开始日期晚于结束日期，区间内不会有数据</p>
          ) : null}
        </div>

        <label className="text-sm text-zinc-500">
          <span className="mb-1 block">策略</span>
          {/* 单选下拉：后端 strategy 只接受一个值，多选会退化成"前端并发拉多次再拼接"，
              那是把单飞与连接数打爆的做法，所以这里老实单选（差异已写进交付报告）。 */}
          <select
            value={value.strategy}
            onChange={(event) => onChange({ ...value, strategy: event.target.value })}
            className="block min-w-56 rounded border border-zinc-300 px-2 py-1.5 text-sm text-zinc-800"
          >
            <option value="">全部策略</option>
            {strategies.map((strategy) => (
              <option key={strategy.name} value={strategy.name}>
                {strategy.name}（{strategy.webhook_key}）
              </option>
            ))}
          </select>
        </label>

        <label className="text-sm text-zinc-500">
          <span className="mb-1 block">股票代码</span>
          <input
            type="text"
            inputMode="numeric"
            value={symbolDraft}
            placeholder="6 位数字"
            onChange={(event) => handleSymbolChange(event.target.value)}
            className={
              symbolRejected
                ? "block w-32 rounded border border-red-400 px-2 py-1.5 text-sm text-zinc-800"
                : "block w-32 rounded border border-zinc-300 px-2 py-1.5 text-sm text-zinc-800"
            }
          />
          {symbolRejected ? (
            <span className="mt-1 block text-xs text-red-700">需为 6 位数字，当前不会发起查询</span>
          ) : null}
        </label>

        {(value.symbol !== "" || value.strategy !== "") && (
          <button
            type="button"
            onClick={() => {
              setSymbolDraft("");
              onChange({ ...value, strategy: "", symbol: "" });
            }}
            className="mb-0.5 rounded border border-zinc-300 px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100"
          >
            清除代码/策略
          </button>
        )}
      </div>
    </section>
  );
}
