import Redis from "ioredis";
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

const redis = new Redis(process.env.REDIS_URL || "redis://localhost:6379");

// Bot 3 reads "config:trading_mode" on every incoming signal, so flipping
// this key is enough to switch the whole system between Co-Pilot and
// Fully Automated — no service restart needed.
export async function POST(req: NextRequest) {
  const { mode } = await req.json();
  if (mode !== "copilot" && mode !== "automated") {
    return NextResponse.json({ error: "mode must be 'copilot' or 'automated'" }, { status: 400 });
  }
  await redis.set("config:trading_mode", mode);
  return NextResponse.json({ mode });
}

export async function GET() {
  const mode = (await redis.get("config:trading_mode")) || "copilot";
  return NextResponse.json({ mode });
}
