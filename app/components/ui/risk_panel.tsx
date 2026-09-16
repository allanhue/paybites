"use client";

import { useEffect, useState } from "react";
import { TradeDecision } from "@/app/lib/use_signal_stream";

const statusColor: Record<TradeDecision["status"], string> = {
  approved: "#35D0A0",
  blocked: "#E8A23D",
  pending_approval: "#6C8CFF",
};

const APPROVAL_TTL_SECONDS = 60;

function secondsLeft(timestamp: number): number {
  const age = (Date.now() - timestamp) / 1000;
  return Math.max(0, Math.round(APPROVAL_TTL_SECONDS - age));
}

export default function RiskPanel({ decisions }: { decisions: TradeDecision[] }) {
  const [, forceTick] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(interval);
  }, []);

  async function approve(d: TradeDecision) {
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: d.symbol,
        confidence: d.confidence,
        trigger_price: d.price,
        timestamp: d.timestamp,
      }),
    });
    if (!res.ok) {
      const text = await res.text();
      alert(text || "Approval failed");
    }
  }

  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">
        risk shield decisions
      </div>
      <div className="max-h-96 overflow-y-auto">
        {decisions.length === 0 && (
          <p className="px-4 py-6 text-sm text-[#7C8B9C]">No decisions yet — Bot 3 is idle.</p>
        )}
        {decisions.map((d, i) => {
          const left = secondsLeft(d.timestamp);
          const expired = left <= 0;
          return (
            <div
              key={i}
              className="border-b border-[#1B2531] px-4 py-3 last:border-b-0"
              style={{ borderLeft: `3px solid ${statusColor[d.status]}` }}
            >
              <div className="flex items-center justify-between text-sm">
                <span className="font-mono text-[#E7ECF2]">{d.symbol}</span>
                <span style={{ color: statusColor[d.status] }}>{d.status.replace("_", " ")}</span>
              </div>
              <p className="mt-1 text-xs text-[#7C8B9C]">{d.reason}</p>
              {d.status === "pending_approval" && (
                <div className="mt-2 flex items-center gap-2">
                  <button
                    onClick={() => approve(d)}
                    disabled={expired}
                    className={`rounded-sm px-3 py-1 text-xs font-medium ${
                      expired
                        ? "cursor-not-allowed bg-[#1B2531] text-[#7C8B9C]"
                        : "bg-[#35D0A0] text-[#0F1720] hover:opacity-90"
                    }`}
                  >
                    {expired ? "Expired" : "Approve & Trade"}
                  </button>
                  {!expired && <span className="font-mono text-xs text-[#7C8B9C]">{left}s left</span>}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}