"use client";

import { useEffect, useState } from "react";

type Bag = Record<string, string>;
type Monitor = { rolling?: Bag; gate?: Bag; gate_counts?: Bag; scores?: Bag };

const stateColor = (v?: string) =>
  v === "tripped" || v === "blocking"
    ? "#E8A23D"
    : v === "ok" || v === "open"
      ? "#35D0A0"
      : "#7C8B9C";

function Row({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex justify-between border-b border-[#1B2531] px-4 py-2 text-sm last:border-b-0">
      <span className="text-[#7C8B9C]">{label}</span>
      <span className="font-mono" style={{ color: color ?? "#E7ECF2" }}>
        {value}
      </span>
    </div>
  );
}

export default function MonitorPanel() {
  const [m, setM] = useState<Monitor>({});
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetch(`${process.env.NEXT_PUBLIC_API_URL}/monitor`)
        .then((r) => r.json())
        .then((d: Monitor) => {
          if (alive) {
            setM(d);
            setFailed(false);
          }
        })
        .catch(() => alive && setFailed(true));
    load();
    const id = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const g = m.gate ?? {};
  const r = m.rolling ?? {};
  const c = m.gate_counts ?? {};
  const expectancy = r.expectancy_net_pct !== undefined ? Number(r.expectancy_net_pct) : null;

  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">
        system health
      </div>
      {failed && (
        <p className="px-4 py-3 text-sm text-[#E8A23D]">
          Cannot reach the gateway Server Down !
        </p>
      )}
      <Row label="market regime (crypto)" value={g.regime_crypto ?? "no data"} color={stateColor(g.regime_crypto)} />
      <Row label="share of symbols falling" value={g.falling_share_crypto ?? "n/a"} />
      <Row
        label="score p50 / p99 / max (threshold)"
        value={
          m.scores?.p50 !== undefined
            ? `${m.scores.p50} / ${m.scores.p99} / ${m.scores.max} (${m.scores.threshold})`
            : "no data"
        }
      />
      <Row label="circuit breaker" value={g.breaker ?? "no data"} color={stateColor(g.breaker)} />
      <Row
        label={`rolling win rate (last ${r.window ?? "200"})`}
        value={r.win_rate !== undefined ? `${r.win_rate}% of ${r.n}` : "no data"}
      />
      <Row
        label="rolling net return / trade"
        value={expectancy !== null ? `${expectancy.toFixed(3)}%` : "no data"}
        color={expectancy === null ? undefined : expectancy >= 0 ? "#35D0A0" : "#E8A23D"}
      />
      <Row label="open tracked entries" value={r.open_entries ?? "0"} />
      <Row
        label="signals blocked (regime / breaker / cooldown)"
        value={`${c.regime ?? 0} / ${c.breaker ?? 0} / ${c.cooldown ?? 0}`}
      />
    </div>
  );
}