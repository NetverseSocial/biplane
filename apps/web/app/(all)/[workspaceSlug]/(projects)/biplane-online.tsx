"use client";
// biplane — top-bar brand + online indicator. Shows the app's name (nowhere else in the chrome)
// with the logo, and a live dot: green when the Plane API answers, red when it doesn't. Sits
// LEFT of the profile circle in the top-nav flex row, so the circle itself never moves.
import { useEffect, useState } from "react";
import { BiplaneLogo } from "./biplane-logo";

const PING_MS = 15000;

export function BiplaneOnline() {
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    const ping = async () => {
      try {
        const r = await fetch("/api/instances/", { credentials: "same-origin" });
        if (alive) setOnline(r.ok);
      } catch {
        if (alive) setOnline(false);
      }
    };
    ping();
    const id = setInterval(ping, PING_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const color = online === null ? "#9aa6b2" : online ? "#22c55e" : "#ef4444";
  const label = online === null ? "checking…" : online ? "online" : "offline";

  return (
    <div
      title={`biplane · ${label}`}
      className="flex flex-shrink-0 items-center gap-1.5 rounded-sm bg-layer-2 px-2.5 py-1.5"
      aria-label={`biplane is ${label}`}
    >
      <BiplaneLogo size={16} />
      <span className="hidden text-11 font-medium md:block">biplane</span>
      <span
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
          boxShadow: online ? "0 0 4px " + color : undefined,
        }}
      />
    </div>
  );
}
