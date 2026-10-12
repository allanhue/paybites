import { ExecutionEvent } from "@/app/lib/use_signal_stream";

const statusColor: Record<string, string> = {
  filled: "#35D0A0",
  closed: "#6C8CFF",
  rejected: "#E8A23D",
  failed: "#E8A23D",
  exit_failed: "#F87171",
};

function detail(e: ExecutionEvent): string {
  if (e.status === "filled") {
    return `entry ${e.entry !== undefined ? e.entry.toFixed(4) : "?"}${e.qty !== undefined ? ` qty ${e.qty}` : ""}`;
  }
  if (e.status === "closed") {
    return `${e.reason ?? "closed"} · pnl ${e.pnl !== undefined ? e.pnl.toFixed(4) : "?"}`;
  }
  return e.reason ?? "";
}

export default function ExecutionLog({ executions }: { executions: ExecutionEvent[] }) {
  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">
        execution log — what the bridge did with your approvals
      </div>
      <div className="max-h-64 overflow-y-auto">
        {executions.length === 0 && (
          <p className="px-4 py-6 text-sm text-[#7C8B9C]">
            Nothing yet. After you approve a trade, the bridge&apos;s answer (filled, rejected and why, closed with
            profit or loss) shows up here.
          </p>
        )}
        {executions.map((e, i) => (
          <div
            key={i}
            className="flex items-start justify-between gap-3 border-b border-[#1B2531] px-4 py-2 text-sm last:border-b-0"
          >
            <span className="font-mono text-xs text-[#7C8B9C]">
              {new Date(e.receivedAt).toLocaleTimeString()}
            </span>
            <span className="font-mono text-[#E7ECF2]">{e.symbol ?? "—"}</span>
            <span style={{ color: statusColor[e.status] ?? "#7C8B9C" }}>{e.status}</span>
            <span className="flex-1 text-right text-xs text-[#7C8B9C]">{detail(e)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}