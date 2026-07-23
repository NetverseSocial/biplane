"use client";
// biplane — the about modal, opened from the bottom-left "Community" (edition) button.
// Top: who WE are — logo, name, what biplane is, our GitHub. Bottom: who we're built on —
// Plane, with their logo, site, and GitHub. Credit where it's due; this fork exists because
// Plane is open source.
import { useEffect } from "react";
import { createPortal } from "react-dom";
import { BiplaneLogo } from "./biplane-logo";

const OUR_GITHUB = "https://github.com/NetverseSocial/biplane";
const PLANE_SITE = "https://plane.so";
const PLANE_GITHUB = "https://github.com/makeplane/plane";

export function BiplaneAboutModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const link = (href: string, label: string) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-11 font-medium underline underline-offset-2 opacity-80 hover:opacity-100"
      style={{ color: "inherit" }}
    >
      {label}
    </a>
  );

  return createPortal(
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100001,
        background: "rgba(0,0,0,.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
      role="dialog"
      aria-modal="true"
      aria-label="About Biplane"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-layer-1 text-primary"
        style={{
          width: "min(440px, 92vw)",
          borderRadius: 12,
          padding: "22px 24px",
          boxShadow: "0 12px 40px rgba(0,0,0,.35)",
        }}
      >
        {/* ours */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
          <BiplaneLogo size={40} />
          <div>
            <div style={{ fontSize: 18, fontWeight: 700, lineHeight: 1.2 }}>Biplane</div>
            <div className="text-11" style={{ opacity: 0.65 }}>
              Two wings. More lift. · Community fork
            </div>
          </div>
        </div>
        <p className="text-13" style={{ margin: "0 0 10px", lineHeight: 1.55, opacity: 0.9 }}>
          Biplane is a multi-agent layer on top of Plane — humans on one wing, agents on the other: agents file, review, and move work
          automatically — commits, pull requests, and merges drive ticket state through a signed
          audit ledger — while humans watch it happen live on the board, the Wheel, and the
          Traveler.
        </p>
        <div style={{ marginBottom: 16 }}>{link(OUR_GITHUB, "github.com/NetverseSocial/biplane")}</div>

        <hr style={{ border: 0, borderTop: "1px solid rgba(128,128,128,.25)", margin: "14px 0" }} />

        {/* theirs */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
          <img
            src="/favicon/android-chrome-192x192.png"
            alt="Plane logo"
            style={{ width: 26, height: 26, borderRadius: 6 }}
          />
          <div style={{ fontSize: 14, fontWeight: 650 }}>Built on Plane</div>
        </div>
        <p className="text-11" style={{ margin: "0 0 8px", lineHeight: 1.5, opacity: 0.75 }}>
          The open-source project management tool this fork grew from (AGPL-3.0). Plane is a
          product of Plane Software, Inc.; Biplane is an independent fork, not affiliated with or
          endorsed by them. Thank you, Plane team.
        </p>
        <div style={{ display: "flex", gap: 14 }}>
          {link(PLANE_SITE, "plane.so")}
          {link(PLANE_GITHUB, "github.com/makeplane/plane")}
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
          <button
            onClick={onClose}
            className="bg-layer-2 text-13"
            style={{ border: 0, borderRadius: 6, padding: "6px 14px", cursor: "pointer", color: "inherit" }}
          >
            Close
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
