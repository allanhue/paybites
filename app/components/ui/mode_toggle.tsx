"use client";

import { useEffect, useState } from "react";

type Mode = "copilot" | "automated";

export default function ModeToggle() {
  const [mode, setMode] = useState<Mode>("copilot");
  const [pending, setPending] = useState(false);

  useEffect(() => {
    fetch(`${process.env.NEXT_PUBLIC_API_URL}/mode`)
      .then((r) => r.json())
      .then((d) => setMode(d.mode))
      .catch(() => {});
  }, []);

  async function flip(next: Mode) {
    if (next === mode || pending) return;
    setPending(true);
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: next }),
      });
      if (res.ok) setMode(next);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex overflow-hidden rounded-md border border-[#233042]">
      <button
        onClick={() => flip("copilot")}
        className={`px-3 py-1.5 text-sm transition-colors ${
          mode === "copilot"
            ? "bg-[#6C8CFF] text-[#0F1720]"
            : "bg-transparent text-[#7C8B9C] hover:text-[#E7ECF2]"
        }`}
      >
        Co-Pilot
      </button>
      <button
        onClick={() => flip("automated")}
        className={`px-3 py-1.5 text-sm transition-colors ${
          mode === "automated"
            ? "bg-[#E8A23D] text-[#0F1720]"
            : "bg-transparent text-[#7C8B9C] hover:text-[#E7ECF2]"
        }`}
      >
        Fully Automated
      </button>
    </div>
  );
}
