"use client";

import { useEffect, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

export default function HistoryChart({ symbol }: { symbol: string }) {
  const [data, setData] = useState<any[]>([]);

  useEffect(() => {
    fetch(`${process.env.NEXT_PUBLIC_API_URL}/history?symbol=${symbol}&limit=200`)
      .then((r) => r.json())
      .then((d) => setData(d.signals.map((s: any) => ({ time: new Date(s.created_at).toLocaleTimeString(), confidence: s.confidence, price: s.price }))));
  }, [symbol]);

  return (
    <div className="border border-[#233042] bg-[#161F2B] p-4">
      <div className="mb-2 text-sm text-[#7C8B9C]">confidence history — {symbol}</div>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data}>
          <XAxis dataKey="time" stroke="#7C8B9C" fontSize={10} tick={false} />
          <YAxis stroke="#7C8B9C" fontSize={10} domain={[0, 100]} />
          <Tooltip contentStyle={{ background: "#161F2B", border: "1px solid #233042" }} />
          <Line type="monotone" dataKey="confidence" stroke="#35D0A0" dot={false} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}