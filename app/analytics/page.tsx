"use client";

import Navbar from "@/app/components/navbar/navbar";
import OutcomesPanel from "@/app/components/ui/outcome_panel";
import HistoryChart from "@/app/components/ui/history_chart";
import BestHoursChart from "@/app/components/ui/best_hours_chart";
import NewsFeed from "@/app/components/ui/news_feed";
import { useSignalStream } from "@/app/lib/use_signal_stream";

export default function AnalyticsPage() {
  const { signals, outcomes, missed, news, connected } = useSignalStream();
  const symbol = signals[0]?.symbol || "BTCUSDT";

  return (
    <div className="min-h-screen bg-[#0F1720] text-[#E7ECF2]">
      <Navbar connected={connected} />
      <main className="mx-auto max-w-6xl space-y-6 p-6">
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <HistoryChart symbol={symbol} />
          <BestHoursChart symbol={symbol} />
        </div>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <OutcomesPanel outcomes={outcomes} title="trade outcomes (real signals)" />
          <OutcomesPanel outcomes={missed} title="missed opportunities (near-misses)" />
        </div>
        <NewsFeed news={news} />
      </main>
    </div>
  );
}