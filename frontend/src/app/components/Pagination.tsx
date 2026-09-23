/**
 * 分页（任务书 §4.4）：基于接口返回的 total/limit/offset，页码 + 每页条数（50/100/200）。
 *
 * 这里只负责"把用户意图翻成 offset"，不负责发请求；
 * 切筛选条件时的 offset 归零发生在父组件（page.tsx 的 applyFilters），
 * 因为那才是"条件变了"这个事件唯一被描述的地方。
 */

"use client";

/** 可选每页条数（与后端 `limit: int = Query(100, ge=1, le=1000)` 兼容）。 */
export const PAGE_SIZE_OPTIONS = [50, 100, 200] as const;

type Props = {
  total: number;
  limit: number;
  offset: number;
  /** 翻页：只给新的 offset。 */
  onPageChange: (offset: number) => void;
  /** 换每页条数：语义含"回到第 1 页"，由父组件一并把 offset 归零（见 page.tsx）。 */
  onLimitChange: (limit: number) => void;
};

export default function Pagination({ total, limit, offset, onPageChange, onLimitChange }: Props) {
  const pageCount = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.min(Math.floor(offset / limit) + 1, pageCount);
  const firstShown = total === 0 ? 0 : offset + 1;
  const lastShown = Math.min(offset + limit, total);

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-zinc-600">
      <span>
        第 {firstShown}–{lastShown} 条 / 共 {total} 条
      </span>

      <div className="flex items-center gap-1">
        <button
          type="button"
          disabled={currentPage <= 1}
          onClick={() => onPageChange((currentPage - 2) * limit)}
          className="rounded border border-zinc-300 px-3 py-1 text-zinc-700 hover:bg-zinc-100 disabled:cursor-not-allowed disabled:text-zinc-400 disabled:hover:bg-white"
        >
          上一页
        </button>
        <span className="px-2">
          第 {currentPage} / {pageCount} 页
        </span>
        <button
          type="button"
          disabled={currentPage >= pageCount}
          onClick={() => onPageChange(currentPage * limit)}
          className="rounded border border-zinc-300 px-3 py-1 text-zinc-700 hover:bg-zinc-100 disabled:cursor-not-allowed disabled:text-zinc-400 disabled:hover:bg-white"
        >
          下一页
        </button>
      </div>

      <label className="flex items-center gap-2">
        每页
        <select
          value={limit}
          onChange={(event) => {
            // 换每页条数即回第 1 页：offset 归零由父组件在 onLimitChange 里一次做完。
            onLimitChange(Number(event.target.value));
          }}
          className="rounded border border-zinc-300 px-2 py-1 text-zinc-800"
        >
          {PAGE_SIZE_OPTIONS.map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </select>
        条
      </label>
    </div>
  );
}
