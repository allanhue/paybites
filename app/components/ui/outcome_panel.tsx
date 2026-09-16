import { TradeOutcome } from "@/app/lib/use_signal_stream";

const color: Record<string, string> = { win: "#35D0A0", loss: "#E8A23D", timeout: "#7C8B9C" };

export default function OutcomesPanel({ outcomes, title }: { outcomes: TradeOutcome[]; title: string }) {
  const resolved = outcomes.filter((o) => o.status === "resolved");
  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">{title}</div>
      <div className="max-h-64 overflow-y-auto">
        {resolved.length === 0 && <p className="px-4 py-6 text-sm text-[#7C8B9C]">Nothing resolved yet.</p>}
        {resolved.map((o, i) => (
          <div key={i} className="flex items-center justify-between border-b border-[#1B2531] px-4 py-2 text-sm last:border-b-0">
            <span className="font-mono text-[#E7ECF2]">{o.symbol}</span>
            <span style={{ color: color[o.outcome || ""] }}>{o.outcome}</span>
            <span className="font-mono text-[#7C8B9C]">{o.pct_change}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}