"use client";

import { useEffect, useState } from "react";
import Navbar from "@/app/components/navbar/navbar";
import OutcomesPanel from "@/app/components/ui/outcome_panel";
import HistoryChart from "@/app/components/ui/history_chart";
import BestHoursChart from "@/app/components/ui/best_hours_chart";
import NewsFeed from "@/app/components/ui/news_feed";
import { useSignalStream, TradeOutcome } from "@/app/lib/use_signal_stream";

export default function AnalyticsPage() {
  const { signals, outcomes, missed, news, connected } = useSignalStream();
  const symbol = signals[0]?.symbol || "BTCUSDT";

  const [historicalOutcomes, setHistoricalOutcomes] = useState<TradeOutcome[]>(
    [],
  );
  const [historicalMissed, setHistoricalMissed] = useState<TradeOutcome[]>([]);

  useEffect(() => {
    fetch(
      `${process.env.NEXT_PUBLIC_API_URL}/history?symbol=${symbol}&limit=50`,
    )
      .then((r) => r.json())
      .then((d) => {
        const rows = (d.outcomes || []).map((o: any) => ({
          symbol: o.symbol,
          kind: o.kind,
          entry_price: o.entry_price,
          exit_price: o.exit_price,
          confidence: o.confidence,
          outcome: o.outcome,
          pct_change: o.pct_change,
          status: "resolved" as const,
        }));
        setHistoricalOutcomes(rows.filter((o: any) => o.kind !== "near_miss"));
        setHistoricalMissed(rows.filter((o: any) => o.kind === "near_miss"));
      })
      .catch(() => {});
  }, [symbol]);

  return (
    <div className="min-h-screen bg-[#0F1720] text-[#E7ECF2]">
      <Navbar connected={connected} />
      <main className="mx-auto max-w-6xl space-y-6 p-6">
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <HistoryChart symbol={symbol} />
          <BestHoursChart symbol={symbol} />
        </div>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <OutcomesPanel
            outcomes={[...outcomes, ...historicalOutcomes]}
            title="trade outcomes (real signals)"
          />
          <OutcomesPanel
            outcomes={[...missed, ...historicalMissed]}
            title="missed opportunities (near-misses)"
          />
        </div>
        <NewsFeed news={news} />
      </main>
    </div>
  );
}
