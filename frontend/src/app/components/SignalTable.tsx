/**
 * 信号表（任务书 §4.4）：同一天多条按日期分组显示，这是"看板"比"裸表格"有价值的地方。
 *
 * 分组只做视觉，不改分页语义：条目顺序与条数完全等于接口返回的 items，
 * 分页仍按 total/limit/offset（后端排序是 trade_date DESC, strategy, symbol, id，
 * 所以同一天的条目天然连续，一次线性扫描即可分组，不需要排序也不需要去重）。
 *
 * 日期列用 rowSpan 合并：既是任务书要求的"日期"列，又让每天只出现一次日期标签，
 * 不产生重复文本，也不需要在行间塞空单元格。
 */

"use client";

import type { SignalItem } from "../lib/types";

type DateGroup = {
  tradeDate: string;
  items: SignalItem[];
};

/** 按 trade_date 聚成连续分组；出现不连续的同日区块时照常分块，不做合并（保持顺序诚实）。 */
function groupByTradeDate(items: SignalItem[]): DateGroup[] {
  const groups: DateGroup[] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last !== undefined && last.tradeDate === item.trade_date) {
      last.items.push(item);
      continue;
    }
    groups.push({ tradeDate: item.trade_date, items: [item] });
  }
  return groups;
}

/** `name` 为 null 表示行情库未收录该代码：显示破折号，绝不显示 undefined 或留空白。 */
function displayName(name: string | null): string {
  return name === null ? "—" : name;
}

export default function SignalTable({ items }: { items: SignalItem[] }) {
  const groups = groupByTradeDate(items);
  return (
    <div className="max-h-[60vh] overflow-auto rounded border border-zinc-200 bg-white">
      <table className="w-full min-w-[720px] border-collapse text-left text-sm">
        <thead className="sticky top-0 bg-zinc-100 text-zinc-600">
          <tr>
            <th className="border-b border-zinc-200 px-4 py-2 font-medium">日期</th>
            <th className="border-b border-zinc-200 px-4 py-2 font-medium">策略</th>
            <th className="border-b border-zinc-200 px-4 py-2 font-medium">代码</th>
            <th className="border-b border-zinc-200 px-4 py-2 font-medium">名称</th>
            <th className="border-b border-zinc-200 px-4 py-2 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) =>
            group.items.map((item, index) => (
              <tr
                // 同日同策略同代码可能因任务重跑出现两条（后端唯一键是 task_id+strategy+symbol），
                // 所以 key 里带组内下标，保证同数组内唯一。
                key={`${group.tradeDate}-${item.strategy}-${item.symbol}-${index}`}
                className="border-b border-zinc-100 hover:bg-zinc-50"
              >
                {index === 0 ? (
                  <td
                    rowSpan={group.items.length}
                    className="border-r border-zinc-100 bg-zinc-50 px-4 py-2 align-top whitespace-nowrap font-medium text-zinc-700"
                  >
                    <span className="block">{group.tradeDate}</span>
                    <span className="block text-xs font-normal text-zinc-500">
                      {group.items.length} 条
                    </span>
                  </td>
                ) : null}
                <td className="px-4 py-2 text-zinc-700" title={item.webhook_key}>
                  {item.strategy}
                </td>
                <td className="px-4 py-2 font-mono text-zinc-800">{item.symbol}</td>
                <td className="px-4 py-2 text-zinc-800">{displayName(item.name)}</td>
                <td className="px-4 py-2 whitespace-nowrap">
                  {/* 雪球看盘外链：xueqiu_code 由后端统一映射（SH/SZ/BJ），前端不再自己拼前缀。 */}
                  <a
                    href={`https://xueqiu.com/S/${item.xueqiu_code}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-700 hover:underline"
                  >
                    雪球 {item.xueqiu_code}
                  </a>
                </td>
              </tr>
            )),
          )}
        </tbody>
      </table>
    </div>
  );
}
