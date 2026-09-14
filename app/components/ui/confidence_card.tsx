import { TradeSignal } from "@/app/lib/use_signal-stream";

export default function ConfidenceCard({ latest }: { latest: TradeSignal | null }) {
  return (
    <div className="border border-[#233042] bg-[#161F2B] p-6">
      <p className="text-sm text-[#7C8B9C]">latest signal confidence</p>
      <div className="mt-2 flex items-baseline gap-3">
        <span className="font-mono text-6xl text-[#E7ECF2]">
          {latest ? latest.confidence.toFixed(1) : "--"}
        </span>
        <span className="text-xl text-[#7C8B9C]">%</span>
      </div>
      <div className="mt-4 flex items-center gap-4 text-sm">
        <span className="text-[#7C8B9C]">symbol</span>
        <span className="font-mono text-[#E7ECF2]">{latest?.symbol ?? "—"}</span>
        <span className="text-[#7C8B9C]">trigger</span>
        <span className="font-mono text-[#E7ECF2]">
          {latest ? latest.trigger_price.toFixed(2) : "—"}
        </span>
      </div>
    </div>
  );
}
