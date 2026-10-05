//create an favcon for te system based on icon tsx
import { ImageResponse } from "next/og";

// Next.js picks this file up automatically and serves it as the site favicon.
export const size = { width: 32, height: 32 };
export const contentType = "image/png";

export default function Icon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "flex-end",
          justifyContent: "center",
          gap: 3,
          paddingBottom: 6,
          background: "#0F1720",
          border: "1px solid #233042",
          borderRadius: 8,
        }}
      >
        {/* three rising bars: the last one is the "signal" in the dashboard's green */}
        <div style={{ width: 5, height: 9, background: "#7C8B9C", borderRadius: 1 }} />
        <div style={{ width: 5, height: 14, background: "#7C8B9C", borderRadius: 1 }} />
        <div style={{ width: 5, height: 20, background: "#35D0A0", borderRadius: 1 }} />
      </div>
    ),
    { ...size },
  );
}