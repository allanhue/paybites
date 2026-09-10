import Redis from "ioredis";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const redisUrl = process.env.REDIS_URL || "redis://localhost:6379";
  const sub = new Redis(redisUrl);

  let cleanup: () => void = () => {};

  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();

      const send = (channel: string, data: string) => {
        const event = `event: ${channel}\ndata: ${data}\n\n`;
        controller.enqueue(encoder.encode(event));
      };

      sub.subscribe("market.signals", "trade.decisions").catch((err) => {
        controller.enqueue(
          encoder.encode(`event: error\ndata: ${JSON.stringify({ message: String(err) })}\n\n`)
        );
      });

      sub.on("message", (channel, message) => {
        send(channel, message);
      });

      const heartbeat = setInterval(() => {
        controller.enqueue(encoder.encode(": ping\n\n"));
      }, 20000);

      cleanup = () => {
        clearInterval(heartbeat);
        sub.disconnect();
      };
    },
    cancel() {
      cleanup();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}