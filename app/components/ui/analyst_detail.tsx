import { ScoreTick } from "@/app/lib/use_signal-stream";

export default function AnalystDetail({ latest }: { latest: ScoreTick | null }) {
  if (!latest) {
    return (
      <div className="border border-[#233042] bg-[#161F2B] p-4 text-sm text-[#7C8B9C]">
        Waiting for the analyst to score a tick...
      </div>
    );
  }
  const bars: [string, number, string][] = [
    ["RSI pressure", latest.components.rsi_component, "#35D0A0"],
    ["Momentum", latest.components.momentum_component, "#6C8CFF"],
    ["Band position", latest.components.band_component, "#E8A23D"],
  ];
  return (
    <div className="border border-[#233042] bg-[#161F2B] p-4">
      <div className="flex items-center justify-between text-sm text-[#7C8B9C]">
        <span>analyst detail — {latest.symbol}</span>
        <span className="font-mono text-[#E7ECF2]">{latest.score.toFixed(1)}%</span>
      </div>
      <div className="mt-3 space-y-2">
        {bars.map(([label, value, color]) => (
          <div key={label}>
            <div className="flex justify-between text-xs text-[#7C8B9C]"><span>{label}</span><span>{value.toFixed(1)}</span></div>
            <div className="h-1.5 w-full bg-[#1B2531]">
              <div className="h-1.5" style={{ width: `${Math.min(100, value)}%`, backgroundColor: color }} />
            </div>
          </div>
        ))}
      </div>
      <div className="mt-3 flex gap-4 text-xs text-[#7C8B9C]">
        <span>RSI {latest.rsi_14.toFixed(1)}</span>
        <span>Momentum {(latest.momentum * 100).toFixed(3)}%</span>
        <span>Band {latest.band_position.toFixed(2)}</span>
        <span>Vol {(latest.volatility * 100).toFixed(3)}%</span>
      </div>
    </div>
  );
}