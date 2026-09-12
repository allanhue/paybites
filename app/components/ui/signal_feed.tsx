import { TradeSignal } from "@/app/lib/useSignalStream";

export default function SignalFeed({ signals }: { signals: TradeSignal[] }) {
  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">
        signal feed
      </div>
      <div className="max-h-96 overflow-y-auto">
        {signals.length === 0 && (
          <p className="px-4 py-6 text-sm text-[#7C8B9C]">
            No signals yet -  waiting on the analyst.
          </p>
        )}
        {signals.map((s, i) => (
          <div
            key={i}
            className="flex items-center justify-between border-b border-[#1B2531] px-4 py-2.5 text-sm last:border-b-0"
          >
            <span className="font-mono text-[#E7ECF2]">{s.symbol}</span>
            <span className="text-[#7C8B9C]">{s.action}</span>
            <span className="font-mono text-[#35D0A0]">{s.confidence.toFixed(1)}%</span>
            <span className="font-mono text-[#7C8B9C]">{s.trigger_price.toFixed(2)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
