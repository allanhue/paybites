"use client";

import { useEffect, useRef, useState } from "react";

export type TradeSignal = {
  symbol: string;
  action: string;
  confidence: number;
  trigger_price: number;
};

export type TradeDecision = {
  symbol: string;
  action: string;
  confidence: number;
  price: number;
  status: "approved" | "blocked" | "pending_approval";
  reason: string;
  timestamp: number;
};

/**
 * Subscribes to /api/signals (SSE) and keeps the last N signals + decisions
 * in memory for the dashboard. Reconnects automatically if the stream drops.
 */
export function useSignalStream(maxItems = 50) {
  const [signals, setSignals] = useState<TradeSignal[]>([]);
  const [decisions, setDecisions] = useState<TradeDecision[]>([]);
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const es = new EventSource("/api/signals");
    esRef.current = es;

    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);

    es.addEventListener("market.signals", (e) => {
      const data = JSON.parse((e as MessageEvent).data) as TradeSignal;
      setSignals((prev) => [data, ...prev].slice(0, maxItems));
    });

    es.addEventListener("trade.decisions", (e) => {
      const data = JSON.parse((e as MessageEvent).data) as TradeDecision;
      setDecisions((prev) => [data, ...prev].slice(0, maxItems));
    });

    return () => es.close();
  }, [maxItems]);

  return { signals, decisions, connected };
}
