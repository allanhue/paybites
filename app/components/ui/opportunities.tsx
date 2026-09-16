import { ScoreTick } from "@/app/lib/use_signal_stream";

export default function Opportunities({ scores }: { scores: ScoreTick[] }) {
  const latestBySymbol = new Map<string, ScoreTick>();
  for (const s of scores) {
    if (!latestBySymbol.has(s.symbol)) latestBySymbol.set(s.symbol, s);
  }
  const ranked = Array.from(latestBySymbol.values()).sort((a, b) => b.score - a.score);

  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">
        opportunity ranking — live across all watched symbols
      </div>
      <div className="max-h-80 overflow-y-auto">
        {ranked.length === 0 && (
          <p className="px-4 py-6 text-sm text-[#7C8B9C]">Waiting for scores across symbols...</p>
        )}
        {ranked.map((s) => (
          <div
            key={s.symbol}
            className="flex items-center justify-between border-b border-[#1B2531] px-4 py-2.5 text-sm last:border-b-0"
          >
            <span className="font-mono text-[#E7ECF2]">{s.symbol}</span>
            <div className="h-1.5 w-24 bg-[#1B2531]">
              <div
                className="h-1.5"
                style={{
                  width: `${Math.min(100, s.score)}%`,
                  backgroundColor: s.score >= s.threshold ? "#35D0A0" : "#7C8B9C",
                }}
              />
            </div>
            <span className="font-mono text-[#E7ECF2]">{s.score.toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}