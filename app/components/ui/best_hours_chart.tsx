"use client";

import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

export default function BestHoursChart({ symbol }: { symbol: string }) {
  const [data, setData] = useState<any[]>([]);

  useEffect(() => {
    fetch(`${process.env.NEXT_PUBLIC_API_URL}/history?symbol=${symbol}`)
      .then((r) => r.json())
      .then((d) => setData(d.byHour.map((h: any) => ({
        hour: `${h.hour}:00`,
        winRate: h.total > 0 ? Math.round((h.wins / h.total) * 100) : 0,
      }))));
  }, [symbol]);

  return (
    <div className="border border-[#233042] bg-[#161F2B] p-4">
      <div className="mb-2 text-sm text-[#7C8B9C]">win rate by hour — {symbol}</div>
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={data}>
          <XAxis dataKey="hour" stroke="#7C8B9C" fontSize={10} />
          <YAxis stroke="#7C8B9C" fontSize={10} domain={[0, 100]} />
          <Tooltip contentStyle={{ background: "#161F2B", border: "1px solid #233042" }} />
          <Bar dataKey="winRate" fill="#6C8CFF" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}