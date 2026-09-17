import { ScoreTick } from "@/app/lib/use_signal_stream";

export default function StatusBanner({ scores }: { scores: ScoreTick[] }) {
  const best = scores.reduce((max, s) => (s.score > max ? s.score : max), 0);
  const threshold = scores[0]?.threshold ?? 75;
  const gap = (threshold - best).toFixed(1);

  return (
    <div className="border border-[#233042] bg-[#161F2B] px-4 py-3 text-sm text-[#7C8B9C]">
      Highest live score right now: <span className="font-mono text-[#E7ECF2]">{best.toFixed(1)}%</span>
      {" "}— needs <span className="font-mono text-[#E7ECF2]">{threshold}%</span> to fire a signal
      {best < threshold && <span> ({gap} points away)</span>}. Empty panels below just mean nothing has
      cleared that bar yet — not a bug.
    </div>
  );
}