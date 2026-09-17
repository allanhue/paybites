"use client";

import Navbar from "@/app/components/navbar/navbar";
import ConfidenceCard from "@/app/components/ui/confidence_card";
import SignalFeed from "@/app/components/ui/signal_feed";
import RiskPanel from "@/app/components/ui/risk_panel";
import AnalystDetail from "@/app/components/ui/analyst_detail";
import Opportunities from "@/app/components/ui/opportunities";
import { useSignalStream } from "@/app/lib/use_signal_stream";
import StatusBanner from "@/app/components/ui/status_banner";


export default function LiveOpsPage() {
  const { signals, decisions, scores, connected } = useSignalStream();

  return (
    <div className="min-h-screen bg-[#0F1720] text-[#E7ECF2]">
      <Navbar connected={connected} />
      <main className="mx-auto max-w-6xl space-y-6 p-6">
        <StatusBanner scores={scores} />
        <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
          <ConfidenceCard latest={signals[0] ?? null} />
          <SignalFeed signals={signals} />
          <RiskPanel decisions={decisions} />
        </div>
        <AnalystDetail latest={scores[0] ?? null} />
        <Opportunities scores={scores} />
      </main>
    </div>
  );
}