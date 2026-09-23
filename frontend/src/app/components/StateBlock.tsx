/**
 * 统一状态块（任务书 §4.4）：加载 / 空结果 / 后端错误 / 未回填四种状态都从这里出，
 * 保证每种状态都有明确中文文案，不出现白屏与"无限 spinner"。
 *
 * 刻意做成"只有一个组件 + 一个 kind 分支"而不是四个组件：状态之间的视觉差异
 * 只有配色与图标，分成四个文件早晚会长出四套不一致的间距。
 */

/** 四种非就绪状态；`ready` 由父组件自己渲染表格，不进这里。 */
export type BoardState =
  | { kind: "loading" }
  | { kind: "empty" }
  | { kind: "error"; message: string; hint?: string }
  | { kind: "not_seeded" };

/** 看板页的完整视图状态。 */
export type ViewState = BoardState | { kind: "ready" };

/** 加载占位行数：贴近一屏的真实条数，避免"骨架很短、数据很长"的跳动。 */
const SKELETON_ROWS = 6;

export default function StateBlock({ state }: { state: BoardState }) {
  if (state.kind === "loading") {
    return (
      <div aria-busy="true" className="space-y-2 py-2">
        <p className="text-sm text-zinc-500">正在加载信号数据…</p>
        {Array.from({ length: SKELETON_ROWS }, (_, index) => (
          // 骨架行无内容也无语义，用索引当 key 是安全的（列表长度固定）。
          <div key={index} className="h-9 animate-pulse rounded bg-zinc-100" />
        ))}
      </div>
    );
  }

  if (state.kind === "not_seeded") {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 p-6">
        <p className="text-base font-medium text-amber-900">本地行情库尚未回填</p>
        <p className="mt-2 text-sm text-amber-800">
          后端返回 409 <code className="rounded bg-amber-100 px-1">database_not_seeded</code>
          ，表示 stock_daily 表一行数据都没有，查询接口无法工作。
        </p>
        <p className="mt-3 text-sm text-amber-800">
          下一步：先执行历史回填（<code className="rounded bg-amber-100 px-1">POST
          /api/tasks/backfill</code> 或 <code className="rounded bg-amber-100 px-1">python
          main.py --backfill</code>），回填完成后再回到本页。
        </p>
      </div>
    );
  }

  if (state.kind === "empty") {
    return (
      <div className="rounded border border-zinc-200 bg-zinc-50 p-6">
        <p className="text-base font-medium text-zinc-700">该条件下没有选股信号</p>
        <p className="mt-2 text-sm text-zinc-500">
          把日期区间往前挪几天、清掉股票代码，或改用「近 30 天」再试一次。
        </p>
      </div>
    );
  }

  return (
    <div className="rounded border border-red-200 bg-red-50 p-6">
      <p className="text-base font-medium text-red-900">加载失败</p>
      <p className="mt-2 text-sm text-red-800">{state.message}</p>
      {state.hint ? <p className="mt-2 text-sm text-red-700">{state.hint}</p> : null}
    </div>
  );
}
