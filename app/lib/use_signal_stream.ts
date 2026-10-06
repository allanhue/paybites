"use client";

import { useEffect, useRef, useState } from "react";

export type NewsItem = {
  headline: string;
  link?: string;
  source?: string;
  sentiment: number;
  symbols: string[];
  timestamp: number;
};
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
export type ScoreTick = {
  symbol: string;
  price: number;
  score: number;
  rsi_14: number;
  momentum: number;
  band_position: number;
  volatility: number;
  macd_hist?: number;
  trend_bias?: number;
  components: Record<string, number>;
  threshold: number;
};
export type TradeOutcome = {
  symbol: string;
  price?: number;
  entry_price?: number;
  exit_price?: number;
  confidence: number;
  outcome?: string;
  pct_change?: number;
  status: "pending" | "resolved";
};
// What the execution bridge reports back after an approved trade.
export type ExecutionEvent = {
  status: string; // filled | closed | rejected | failed | exit_failed
  symbol?: string;
  reason?: string;
  pnl?: number;
  entry?: number;
  exit?: number;
  qty?: number;
  broker?: string;
  receivedAt: number;
};

function notify(title: string, body: string) {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  if (Notification.permission === "granted") {
    new Notification(title, { body });
  }
}

export function useSignalStream(maxItems = 50) {
  const [signals, setSignals] = useState<TradeSignal[]>([]);
  const [decisions, setDecisions] = useState<TradeDecision[]>([]);
  const [scores, setScores] = useState<ScoreTick[]>([]);
  const [outcomes, setOutcomes] = useState<TradeOutcome[]>([]);
  const [missed, setMissed] = useState<TradeOutcome[]>([]);
  const [executions, setExecutions] = useState<ExecutionEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const [news, setNews] = useState<NewsItem[]>([]);

  useEffect(() => {
    if (
      typeof window !== "undefined" &&
      "Notification" in window &&
      Notification.permission === "default"
    ) {
      Notification.requestPermission();
    }

    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8090";
    const es = new EventSource(`${apiUrl}/events`);
    esRef.current = es;
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);

    es.addEventListener("market.signals", (e) => {
      const data = JSON.parse((e as MessageEvent).data) as TradeSignal;
      setSignals((prev) => [data, ...prev].slice(0, maxItems));
      notify(
        "Trade signal",
        `${data.symbol} ${data.action} — ${data.confidence}% confidence`,
      );
    });

    es.addEventListener("trade.decisions", (e) => {
      setDecisions((prev) =>
        [JSON.parse((e as MessageEvent).data), ...prev].slice(0, maxItems),
      );
    });

    es.addEventListener("market.scores", (e) => {
      setScores((prev) =>
        [JSON.parse((e as MessageEvent).data), ...prev].slice(0, maxItems),
      );
    });

    es.addEventListener("trade.outcomes", (e) => {
      const data = JSON.parse((e as MessageEvent).data) as TradeOutcome;
      setOutcomes((prev) => [data, ...prev].slice(0, maxItems));
      if (data.status === "resolved") {
        notify(`Trade ${data.outcome}`, `${data.symbol}: ${data.pct_change}%`);
      }
    });

    es.addEventListener("trade.missed", (e) => {
      setMissed((prev) =>
        [JSON.parse((e as MessageEvent).data), ...prev].slice(0, maxItems),
      );
    });

    es.addEventListener("trade.executed", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data) as Omit<ExecutionEvent, "receivedAt">;
        setExecutions((prev) => [{ ...data, receivedAt: Date.now() }, ...prev].slice(0, maxItems));
        notify(`Trade ${data.status}`, `${data.symbol ?? ""} ${data.reason ?? ""}`.trim());
      } catch (err) {
        console.error("Failed to parse trade.executed event", err);
      }
    });

    es.addEventListener("market.news", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data) as NewsItem;
        const item: NewsItem = {
          ...data,
          sentiment: Number(data.sentiment ?? 0),
          symbols: Array.isArray(data.symbols) ? data.symbols : [],
          timestamp: Number(data.timestamp ?? Date.now()),
        };
        setNews((prev) => [item, ...prev].slice(0, maxItems));
      } catch (err) {
        console.error("Failed to parse market.news event", err);
      }
    });

    return () => es.close();
  }, [maxItems]);

  return { signals, decisions, scores, outcomes, missed, executions, news, connected };
}