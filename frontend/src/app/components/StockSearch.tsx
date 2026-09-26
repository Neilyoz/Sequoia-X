"use client";

import { useEffect, useState } from "react";

export default function StockSearch({ onChange }: { onChange: (keyword: string) => void }) {
  const [value, setValue] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => onChange(value.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [onChange, value]);

  return (
    <label className="block">
      <span className="sr-only">按股票代码或名称搜索</span>
      <input
        type="search"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="搜索股票代码或名称"
        className="w-full rounded border border-zinc-300 px-3 py-2 text-sm text-zinc-900 outline-none placeholder:text-zinc-400 focus:border-zinc-500"
      />
    </label>
  );
}
