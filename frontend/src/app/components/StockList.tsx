"use client";

import type { StockListItem } from "../lib/types";

type Props = {
  items: StockListItem[];
  selectedSymbol: string | null;
  onSelect: (stock: StockListItem) => void;
};

export default function StockList({ items, selectedSymbol, onSelect }: Props) {
  return (
    <ul className="divide-y divide-zinc-100">
      {items.map((stock) => {
        const selected = stock.symbol === selectedSymbol;
        return (
          <li key={stock.symbol}>
            <button
              type="button"
              aria-pressed={selected}
              onClick={() => onSelect(stock)}
              className={`flex w-full items-center justify-between gap-3 px-3 py-3 text-left transition hover:bg-zinc-50 ${
                selected ? "bg-zinc-100" : "bg-white"
              }`}
            >
              <span className="font-mono text-sm text-zinc-700">{stock.symbol}</span>
              <span className="min-w-0 flex-1 truncate text-right text-sm font-medium text-zinc-900">
                {stock.name}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
