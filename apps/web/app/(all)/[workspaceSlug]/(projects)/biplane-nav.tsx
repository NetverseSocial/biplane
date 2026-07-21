import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { BiplaneWheel } from "./biplane-wheel";
import { BiplaneTraveler } from "./biplane-traveler";

// biplane native nav — sidebar buttons that open a right-hand slide-in panel (like Plane's
// Analyze pane) with the Wheel / Traveler rendered NATIVELY (no iframe, no separate servers):
// the panels fetch Plane's own API / the ledger proxy same-origin. Theme-aware: follows
// Plane's light/dark and hands the theme straight to the panels.
type View = "wheel" | "traveler";

function usePlaneTheme(): "light" | "dark" {
  const [theme, setTheme] = useState<"light" | "dark">("dark");
  useEffect(() => {
    const detect = () => {
      try {
        // Plane's authoritative signal is data-theme on <html> ("dark" | "light" |
        // "dark-contrast" | "light-contrast" | "custom"). Use it directly — do NOT infer
        // from background luminance: Plane now paints in oklch(), so a rgb()-digit regex
        // gives nonsense and always reads "light" (the theme-stuck bug).
        const el = document.documentElement;
        const dt = (el.getAttribute("data-theme") || "").toLowerCase();
        // Fast path for the named light/dark themes (incl. *-contrast variants).
        if (dt.includes("dark")) return setTheme("dark");
        if (dt.includes("light")) return setTheme("light");
        // "custom" (or anything else): decide from the ACTUAL rendered background. Plane
        // paints in oklch() and a custom theme is a brand+neutral pair under a light|dark
        // mode, so we can't string-match — resolve whatever colour it is to RGB via a
        // canvas (handles oklch/hsl/hex/named) and use luminance.
        const bg =
          getComputedStyle(document.body).backgroundColor ||
          getComputedStyle(el).backgroundColor ||
          "";
        const cv = document.createElement("canvas");
        cv.width = cv.height = 1;
        const ctx = cv.getContext("2d");
        if (ctx && bg) {
          ctx.fillStyle = "#808080"; // neutral default if bg is unparseable
          ctx.fillStyle = bg; // browser resolves any CSS colour format to RGB
          ctx.fillRect(0, 0, 1, 1);
          const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
          const lum = 0.299 * r + 0.587 * g + 0.114 * b;
          return setTheme(lum < 128 ? "dark" : "light");
        }
        // last resort: OS preference
        return setTheme(
          window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
            ? "dark"
            : "light",
        );
      } catch {
        /* keep prior */
      }
    };
    detect();
    const obs = new MutationObserver(detect);
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme", "style"],
    });
    return () => obs.disconnect();
  }, []);
  return theme;
}

export function BiplaneNav() {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<View>("wheel");
  const [wide, setWide] = useState(false);
  const theme = usePlaneTheme();
  const dark = theme === "dark";

  const openView = useCallback((v: View) => {
    setView(v);
    setOpen(true);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const line = dark ? "rgba(255,255,255,.14)" : "rgba(0,0,0,.12)";
  const hov = dark ? "rgba(255,255,255,.10)" : "rgba(0,0,0,.06)";
  const bg = dark ? "#0f1117" : "#ffffff";

  const navBtn = (v: View, label: string) => (
    <button
      key={v}
      onClick={() => openView(v)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        width: "100%",
        padding: "6px 10px",
        borderRadius: 6,
        fontSize: 13,
        color: "inherit",
        background: "transparent",
        border: 0,
        cursor: "pointer",
        textAlign: "left",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = hov)}
      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
    >
      {label}
    </button>
  );

  const tab = (v: View, label: string) => (
    <button
      key={v}
      onClick={() => setView(v)}
      style={{
        border: 0,
        background: view === v ? hov : "transparent",
        color: "inherit",
        cursor: "pointer",
        padding: "6px 12px",
        borderRadius: 6,
        fontWeight: 600,
        fontSize: 13,
        opacity: view === v ? 1 : 0.6,
      }}
    >
      {label}
    </button>
  );

  const icoBtn = (label: string, title: string, fn: () => void) => (
    <button
      title={title}
      onClick={fn}
      style={{
        border: 0,
        background: "transparent",
        color: "inherit",
        cursor: "pointer",
        fontSize: 15,
        lineHeight: 1,
        padding: "6px 9px",
        borderRadius: 6,
        opacity: 0.65,
      }}
    >
      {label}
    </button>
  );

  return (
    <>
      <div data-biplane-nav style={{ borderTop: `1px solid ${line}`, margin: "6px 8px", paddingTop: 4 }}>
        <div style={{ padding: "4px 8px", fontSize: 10, letterSpacing: ".08em", opacity: 0.5 }}>BIPLANE</div>
        {navBtn("wheel", "🎡  Wheel")}
        {navBtn("traveler", "🧭  Traveler")}
      </div>
      {open &&
        createPortal(
          <div
            style={{
              position: "fixed",
              top: 0,
              right: 0,
              height: "100vh",
              width: wide ? "96vw" : "min(46vw,760px)",
              maxWidth: "98vw",
              zIndex: 100000,
              background: bg,
              color: "inherit",
              borderLeft: `1px solid ${line}`,
              boxShadow: "-8px 0 30px rgba(0,0,0,.35)",
              display: "flex",
              flexDirection: "column",
              transition: "width .2s ease",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 4, padding: "6px 8px", borderBottom: `1px solid ${line}` }}>
              {tab("wheel", "🎡 Wheel")}
              {tab("traveler", "🧭 Traveler")}
              <div style={{ flex: 1 }} />
              {icoBtn("⤢", "Expand", () => setWide((w) => !w))}
              {icoBtn("✕", "Close", () => setOpen(false))}
            </div>
            <div style={{ flex: 1, minHeight: 0, overflow: "auto", background: bg }}>
              {view === "wheel" ? <BiplaneWheel theme={theme} /> : <BiplaneTraveler theme={theme} />}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
