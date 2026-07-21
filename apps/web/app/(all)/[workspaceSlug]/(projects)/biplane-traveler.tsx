"use client";
// biplane — native Traveler panel. Replaces the old iframe to a standalone :8793 server: the pure
// fold (biplane-timeline.ts, ported verbatim from the traveler-view module) runs in the browser,
// fed by the Audit Ledger through the AIO proxy's same-origin /biplane-ledger/* route (Caddy
// injects the read bearer server-side — no token ever reaches this code). Theme comes straight
// from the host — no ?theme round-trip.
import { useEffect, useState } from "react";
import {
  foldTimelines,
  type AuditEvent,
  type GroupKind,
  type TimelineGroup,
} from "./biplane-timeline";

const LEDGER = "/biplane-ledger";
const PAGE = 1000; // ledger caps limit at 1000
const MAX_PAGES = 20; // safety valve: 20k events is far beyond a dev ledger
const REFRESH_MS = 10000;

// Pull the whole ledger, paginating on seq. The ledger's GET /events returns
// { events: AuditEvent[], last_seq } ordered by seq ascending.
async function loadEvents(): Promise<AuditEvent[]> {
  const out: AuditEvent[] = [];
  let after = 0;
  for (let i = 0; i < MAX_PAGES; i++) {
    const res = await fetch(`${LEDGER}/events?after=${after}&limit=${PAGE}`, {
      credentials: "same-origin",
    });
    if (!res.ok) throw new Error(`ledger ${res.status}`);
    const j = await res.json();
    const evs: AuditEvent[] = Array.isArray(j?.events) ? j.events : [];
    out.push(...evs);
    if (evs.length < PAGE) break;
    after = evs[evs.length - 1]!.seq;
  }
  return out;
}

// Human names for work items, mined from the webhook payloads themselves (the old server did
// this by calling Plane's API; the payloads already carry the name on issue events, so the
// panel needs no extra credential). Last write wins — renames follow the newest event.
function itemNames(events: AuditEvent[]): Map<string, string> {
  const names = new Map<string, string>();
  for (const e of events) {
    if (String(e.event) !== "issue") continue;
    const d: any = (e.payload as any)?.data ?? {};
    const id = String(d.id ?? "");
    if (id && typeof d.name === "string" && d.name) names.set(id, d.name);
  }
  return names;
}

function fmt(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function BiplaneTraveler({ theme }: { theme: "light" | "dark" }) {
  const [groupBy, setGroupBy] = useState<GroupKind>("item");
  const [groups, setGroups] = useState<TimelineGroup[]>([]);
  const [names, setNames] = useState<Map<string, string>>(new Map());
  const [selected, setSelected] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const events = await loadEvents();
        if (!alive) return;
        setNames(itemNames(events));
        setGroups(foldTimelines(events, groupBy));
        setErr(null);
        setLoaded(true);
      } catch (e) {
        if (alive) {
          setErr((e as Error).message);
          setLoaded(true);
        }
      }
    };
    tick();
    const id = setInterval(tick, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [groupBy]);

  const light = theme === "light";
  const mut = light ? "#656d76" : "#8b98a5";
  const border = light ? "#d0d7de" : "#2b333d";
  const panel = light ? "#ffffff" : "#161b22";
  const accent = light ? "#0969da" : "#4c8dff";
  const ok = light ? "#1a7f37" : "#3fb950";
  const warn = light ? "#9a6700" : "#d29922";
  const line = light ? "#d0d7de" : "#30363d";

  // The selected group, kept stable across refreshes; default to the most recently active.
  const current = groups.find((g) => g.key === selected) ?? groups[0];

  const nameFor = (g: TimelineGroup): string =>
    g.groupBy === "item" ? (names.get(g.key) ?? g.key) : g.key;

  const toggleBtn = (g: GroupKind, label: string) => (
    <button
      key={g}
      onClick={() => {
        setGroupBy(g);
        setSelected(null);
      }}
      style={{
        border: 0,
        padding: "5px 10px",
        font: "inherit",
        fontSize: 12,
        cursor: "pointer",
        background: groupBy === g ? accent : "transparent",
        color: groupBy === g ? "#fff" : "inherit",
      }}
    >
      {label}
    </button>
  );

  const badge = (text: string, color?: string) => (
    <span
      key={text}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "2px 8px",
        borderRadius: 999,
        border: `1px solid ${color ?? border}`,
        background: panel,
        color: color ?? "inherit",
        fontSize: 12,
      }}
    >
      {text}
    </span>
  );

  const dotColor = (kind: string) =>
    kind === "filed" ? mut : kind === "done" ? ok : kind === "comment" ? warn : accent;

  return (
    <div style={{ padding: "12px 16px", overflow: "auto", height: "100%", fontSize: 13, color: "inherit" }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <b style={{ fontSize: 15 }}>biplane · Traveler</b>
        <span style={{ color: mut, fontSize: 12 }}>the per-item medical record</span>
        <span style={{ flex: 1 }} />
        <span style={{ display: "inline-flex", border: `1px solid ${border}`, borderRadius: 6, overflow: "hidden" }}>
          {toggleBtn("item", "By item")}
          {toggleBtn("module", "By module")}
        </span>
        <select
          value={current?.key ?? ""}
          onChange={(e) => setSelected(e.target.value)}
          style={{
            background: panel,
            color: "inherit",
            border: `1px solid ${border}`,
            borderRadius: 6,
            padding: "5px 8px",
            font: "inherit",
            fontSize: 12,
            maxWidth: "45%",
          }}
        >
          {groups.map((g) => (
            <option key={g.key} value={g.key}>
              {(g.done ? "✔ " : "") + nameFor(g) + " · " + g.entries.length + " events"}
            </option>
          ))}
        </select>
      </div>

      {err && (
        <div style={{ background: "rgba(239,83,80,.12)", border: "1px solid #ef5350", borderRadius: 8, padding: "8px 12px", marginBottom: 10, fontSize: 12.5 }}>
          ⚠ couldn't reach the audit ledger: {err}
          {/^ledger (401|503)$/.test(err) && (
            <div style={{ color: mut, marginTop: 4 }}>
              The proxy's ledger read credential isn't configured — check the AIO Caddy route and the ledger's READ_TOKEN.
            </div>
          )}
        </div>
      )}

      {!err && loaded && groups.length === 0 && (
        <div style={{ color: mut, padding: "40px 0", textAlign: "center" }}>No records in the ledger yet.</div>
      )}
      {!loaded && <div style={{ color: mut, padding: "40px 0", textAlign: "center" }}>Loading timeline…</div>}

      {current && (
        <>
          <h2 style={{ margin: "0 0 6px", fontSize: 15 }}>{nameFor(current)}</h2>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 12px", marginBottom: 16, color: mut, fontSize: 12, alignItems: "center" }}>
            {badge(current.done ? "done" : "open", current.done ? ok : warn)}
            {current.latest_state && badge(`state: ${current.latest_state}`)}
            {current.module && current.groupBy === "item" && badge(`module: ${current.module}`)}
            {badge(
              current.verified ? "✓ all signatures verified" : "⚠ unverified events",
              current.verified ? ok : warn,
            )}
            <span>
              seq {current.first_seq}–{current.last_seq}
            </span>
            <span>{current.actors.join(", ")}</span>
          </div>

          {/* the vertical timeline: a rail plus one card per entry */}
          <ul style={{ position: "relative", margin: 0, padding: "0 0 0 26px", listStyle: "none" }}>
            <span
              style={{ position: "absolute", left: 7, top: 6, bottom: 6, width: 2, background: line }}
            />
            {current.entries.map((e) => (
              <li
                key={e.seq}
                style={{
                  position: "relative",
                  padding: "10px 12px 12px 14px",
                  marginBottom: 10,
                  background: panel,
                  border: `1px solid ${border}`,
                  borderRadius: 8,
                }}
              >
                <span
                  style={{
                    position: "absolute",
                    left: -23,
                    top: 16,
                    width: 12,
                    height: 12,
                    borderRadius: "50%",
                    background: dotColor(e.kind),
                    border: `2px solid ${light ? "#f6f8fa" : "#0e1116"}`,
                  }}
                />
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "baseline" }}>
                  <span style={{ fontWeight: 600 }}>{e.label}</span>
                  <span title={e.signature_ok ? "signature verified at ingest" : "signature NOT verified"} style={{ color: e.signature_ok ? ok : warn }}>
                    {e.signature_ok ? "✓" : "⚠"}
                  </span>
                  <span style={{ color: mut }}>by {e.actor}</span>
                  <span style={{ marginLeft: "auto", color: mut, fontSize: 12, fontVariantNumeric: "tabular-nums" }}>{fmt(e.ts)}</span>
                </div>
                <div style={{ color: mut, fontSize: 12, marginTop: 2 }}>
                  <code style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }}>
                    {e.event}/{e.action}
                  </code>{" "}
                  · seq {e.seq}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
