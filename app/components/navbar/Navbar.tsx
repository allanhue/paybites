"use client";

import ModeToggle from "../ui/mode_toggle";

export default function Navbar({ connected }: { connected: boolean }) {
  return (
    <header className="flex items-center justify-between border-b border-[#233042] bg-[#0F1720] px-6 py-4">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-lg tracking-tight text-[#E7ECF2]">paybites</span>
        <span className="text-sm text-[#7C8B9C]">live market desk</span>
      </div>

      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2 text-sm text-[#7C8B9C]">
          <span
            className={`h-2 w-2 rounded-full ${
              connected ? "bg-[#35D0A0]" : "bg-[#E8A23D]"
            }`}
          />
          {connected ? "streaming" : "reconnecting"}
        </div>
        <ModeToggle />
      </div>
    </header>
  );
}
