"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import ModeToggle from "../ui/mode_toggle";

export default function Navbar({ connected }: { connected: boolean }) {
  const pathname = usePathname();
  const linkClass = (path: string) =>
    `px-3 py-1.5 text-sm rounded-sm ${
      pathname === path ? "bg-[#1B2531] text-[#E7ECF2]" : "text-[#7C8B9C] hover:text-[#E7ECF2]"
    }`;

  return (
    <header className="flex items-center justify-between border-b border-[#233042] bg-[#0F1720] px-6 py-4">
      <div className="flex items-center gap-6">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-lg tracking-tight text-[#E7ECF2]">paybites</span>
          <span className="text-sm text-[#7C8B9C]">live market desk</span>
        </div>
        <nav className="flex gap-1">
          <Link href="/" className={linkClass("/")}>Live Ops</Link>
          <Link href="/analytics" className={linkClass("/analytics")}>Analytics & News</Link>
        </nav>
      </div>

      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2 text-sm text-[#7C8B9C]">
          <span className={`h-2 w-2 rounded-full ${connected ? "bg-[#35D0A0]" : "bg-[#E8A23D]"}`} />
          {connected ? "streaming" : "reconnecting"}
        </div>
        <ModeToggle />
      </div>
    </header>
  );
}