// Reference wiring for app/page.tsx — merge this into your existing page,
// don't just overwrite it, since you already have layout/content there.
"use client";

import Navbar from "@/app/components/navbar/Navbar";
import ConfidenceCard from "@/app/components/ui/ConfidenceCard";
import SignalFeed from "@/app/components/ui/SignalFeed";
import RiskPanel from "@/app/components/ui/RiskPanel";
import { useSignalStream } from "@/app/lib/useSignalStream";

export default function DashboardPage() {
  const { signals, decisions, connected } = useSignalStream();

  return (
    <div className="min-h-screen bg-[#0F1720] text-[#E7ECF2]">
      <Navbar connected={connected} />
      <main className="mx-auto grid max-w-6xl grid-cols-1 gap-6 p-6 md:grid-cols-3">
        <div className="md:col-span-1">
          <ConfidenceCard latest={signals[0] ?? null} />
        </div>
        <div className="md:col-span-1">
          <SignalFeed signals={signals} />
        </div>
        <div className="md:col-span-1">
          <RiskPanel decisions={decisions} />
        </div>
      </main>
    </div>
  );
}
