"use client";

import Navbar from "@/app/components/navbar/navbar";
import ConfidenceCard from "@/app/components/ui/confidence_card";
import SignalFeed from "@/app/components/ui/signal_feed";
import RiskPanel from "@/app/components/ui/risk_panel";
import AnalystDetail from "@/app/components/ui/analyst_detail";
import OutcomesPanel from "@/app/components/ui/outcome_panel";
import HistoryChart from "@/app/components/ui/history_chart";
import BestHoursChart from "@/app/components/ui/best_hours-chart";
import Opportunities from "@/app/components/ui/opportunities";
import NewsFeed from "@/app/components/ui/news_feed";




import { useSignalStream } from "@/app/lib/use_signal-stream";

export default function DashboardPage() {
  const { signals, decisions, scores, outcomes, missed,news,  connected } = useSignalStream();
  const symbol = signals[0]?.symbol || "BTCUSDT";

  return (
    <div className="min-h-screen bg-[#0F1720] text-[#E7ECF2]">
      <Navbar connected={connected} />
      <main className="mx-auto max-w-6xl space-y-6 p-6">
        <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
          <ConfidenceCard latest={signals[0] ?? null} />
          <SignalFeed signals={signals} />
          <RiskPanel decisions={decisions} />
        </div>
        <AnalystDetail latest={scores[0] ?? null} />
        <Opportunities scores={scores} />
        <NewsFeed news={news} />
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <HistoryChart symbol={symbol} />
          <BestHoursChart symbol={symbol} />
        </div>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <OutcomesPanel outcomes={outcomes} title="trade outcomes (real signals)" />
          <OutcomesPanel outcomes={missed} title="missed opportunities (near-misses)" />
        </div>
      </main>
    </div>
  );
}