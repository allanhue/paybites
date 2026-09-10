import { TradeDecision } from "@/app/lib/useSignalStream";

const statusColor: Record<TradeDecision["status"], string> = {
  approved: "#35D0A0",
  blocked: "#E8A23D",
  pending_approval: "#6C8CFF",
};

export default function RiskPanel({ decisions }: { decisions: TradeDecision[] }) {
  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">
        risk shield decisions
      </div>
      <div className="max-h-96 overflow-y-auto">
        {decisions.length === 0 && (
          <p className="px-4 py-6 text-sm text-[#7C8B9C]">
            No decisions yet — Bot 3 is idle.
          </p>
        )}
        {decisions.map((d, i) => (
          <div
            key={i}
            className="border-b border-[#1B2531] px-4 py-3 last:border-b-0"
            style={{ borderLeft: `3px solid ${statusColor[d.status]}` }}
          >
            <div className="flex items-center justify-between text-sm">
              <span className="font-mono text-[#E7ECF2]">{d.symbol}</span>
              <span style={{ color: statusColor[d.status] }}>
                {d.status.replace("_", " ")}
              </span>
            </div>
            <p className="mt-1 text-xs text-[#7C8B9C]">{d.reason}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
