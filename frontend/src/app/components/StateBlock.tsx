/** 统一展示列表与行情的加载、空结果和错误状态。 */

export type BoardState =
  | { kind: "loading"; message?: string }
  | { kind: "empty"; title?: string; detail?: string }
  | { kind: "error"; message: string; hint?: string }
  | { kind: "not_seeded" };

export type ViewState = BoardState | { kind: "ready" };

export default function StateBlock({ state }: { state: BoardState }) {
  if (state.kind === "loading") {
    return (
      <div aria-busy="true" className="space-y-2 py-4">
        <p className="text-sm text-zinc-500">{state.message ?? "正在加载…"}</p>
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} className="h-9 animate-pulse rounded bg-zinc-100" />
        ))}
      </div>
    );
  }

  if (state.kind === "not_seeded") {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 p-5">
        <p className="text-base font-medium text-amber-900">本地股票清单尚未同步</p>
        <p className="mt-2 text-sm text-amber-800">
          股票基础信息表为空。请先运行 <code className="rounded bg-amber-100 px-1">python main.py --names</code>，
          再按需运行 <code className="rounded bg-amber-100 px-1">python main.py --backfill</code> 获取本地日线。
        </p>
      </div>
    );
  }

  if (state.kind === "empty") {
    return (
      <div className="rounded border border-zinc-200 bg-zinc-50 p-5">
        <p className="text-base font-medium text-zinc-700">{state.title ?? "没有匹配的股票"}</p>
        {state.detail ? <p className="mt-2 text-sm text-zinc-500">{state.detail}</p> : null}
      </div>
    );
  }

  return (
    <div className="rounded border border-red-200 bg-red-50 p-5">
      <p className="text-base font-medium text-red-900">加载失败</p>
      <p className="mt-2 text-sm text-red-800">{state.message}</p>
      {state.hint ? <p className="mt-2 text-sm text-red-700">{state.hint}</p> : null}
    </div>
  );
}
