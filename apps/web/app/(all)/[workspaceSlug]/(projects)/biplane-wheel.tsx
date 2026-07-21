"use client";
// biplane — native Wheel panel. Replaces the old iframe to a standalone :8791 server: the pure
// geometry (biplane-wheel-geometry.ts, ported verbatim from the enhanced-wheel module) runs in the
// browser, fed by Plane's OWN internal API over the logged-in session (no token, no extra service,
// no iframe). Theme comes straight from the host — no ?theme round-trip.
import { useEffect, useState } from "react";
import { biplaneConfig } from "@/biplane-config";
import {
  buildWheelModel,
  renderWheelSVG,
  BUCKET_COLOR,
  BUCKET_LABEL,
  type PlaneIssue,
  type WheelModel,
} from "./biplane-wheel-geometry";

// Fetch the project's issues + states from Plane's internal API (same-origin, cookie-authed — the
// same surface the rest of the web app uses) and shape them for the pure wheel geometry. State
// detail (group/colour/name) is joined from /states/ since the issue list carries only a state id.
async function loadIssues(ws: string, project: string): Promise<PlaneIssue[]> {
  const base = `/api/workspaces/${ws}/projects/${project}`;
  const [iRes, sRes] = await Promise.all([
    fetch(`${base}/issues/?per_page=100`, { credentials: "same-origin" }),
    fetch(`${base}/states/`, { credentials: "same-origin" }),
  ]);
  if (!iRes.ok) throw new Error(`issues ${iRes.status}`);
  if (!sRes.ok) throw new Error(`states ${sRes.status}`);
  const iJson = await iRes.json();
  const sJson = await sRes.json();
  const issues: any[] = Array.isArray(iJson) ? iJson : (iJson?.results ?? []);
  const states: any[] = Array.isArray(sJson) ? sJson : (sJson?.results ?? []);
  const stateById = new Map(states.map((s) => [String(s.id), s]));
  return issues.map((i) => {
    const st = stateById.get(String(i.state ?? i.state_id ?? ""));
    return {
      id: String(i.id),
      sequence_id: Number(i.sequence_id),
      name: i.name ?? "",
      priority: i.priority ?? "none",
      state: st?.id,
      state_detail: st ? { name: st.name, group: st.group, color: st.color } : undefined,
      created_at: i.created_at,
      updated_at: i.updated_at,
      module_ids: i.module_ids ?? [],
      assignees: i.assignee_ids ?? i.assignees ?? [],
    } as PlaneIssue;
  });
}

function Swatch({ color }: { color: string }) {
  return (
    <i
      style={{
        display: "inline-block",
        width: 11,
        height: 11,
        borderRadius: 3,
        marginRight: 5,
        verticalAlign: "middle",
        background: color,
      }}
    />
  );
}

// Derive the current workspace slug + project id from the URL — Plane's project routes are
// `/<workspaceSlug>/projects/<projectId>/...`. Project-level context (the natural home for the
// module-lane wheel); returns null when not on a project page.
function currentContext(): { ws: string; project: string } | null {
  const m = /\/([^/]+)\/projects\/([0-9a-fA-F-]{36})/.exec(
    typeof window === "undefined" ? "" : window.location.pathname,
  );
  return m ? { ws: m[1]!, project: m[2]! } : null;
}

export function BiplaneWheel({ theme }: { theme: "light" | "dark" }) {
  const [model, setModel] = useState<WheelModel | null>(null);
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [noProject, setNoProject] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const ctx = currentContext();
      if (!ctx) {
        if (alive) setNoProject(true);
        return;
      }
      try {
        const issues = await loadIssues(ctx.ws, ctx.project);
        if (!alive) return;
        setNoProject(false);
        const m = buildWheelModel(issues);
        setModel(m);
        setSvg(renderWheelSVG(m, theme));
        setErr(null);
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
    };
    tick();
    let id: ReturnType<typeof setInterval> | undefined;
    biplaneConfig().then((cfg) => {
      if (alive) id = setInterval(tick, cfg.wheelRefreshMs); // interval from /biplane-config.json
    });
    return () => {
      alive = false;
      if (id) clearInterval(id);
    };
  }, [theme]);

  if (noProject) {
    return (
      <div style={{ padding: 20, color: theme === "light" ? "#5b6673" : "#9aa6b2", fontSize: 13 }}>
        Open a project to see its wheel.
      </div>
    );
  }

  const mut = theme === "light" ? "#5b6673" : "#9aa6b2";
  const hasMovement = !!model?.slices.some((s) => s.changed);

  return (
    <div style={{ padding: "12px 16px", overflow: "auto", height: "100%", fontSize: 13, color: "inherit" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 8 }}>
        <b style={{ fontSize: 15 }}>biplane · Enhanced Wheel</b>
        {model && <span style={{ color: mut, fontSize: 12 }}>{model.count} tickets</span>}
      </div>
      {err && (
        <div style={{ background: "rgba(239,83,80,.12)", border: "1px solid #ef5350", borderRadius: 8, padding: "8px 12px", marginBottom: 10, fontSize: 12.5 }}>
          ⚠ couldn't load Plane data: {err}
        </div>
      )}
      {/* legends — colours match the board's states */}
      <div style={{ color: mut, fontSize: 12.5, marginBottom: 4 }}>
        slice = ticket · width = priority ·{" "}
        {hasMovement ? "inner ring = state ~24h ago · middle ring = state now" : "ring colour = current state (same as board)"}{" "}
        · outer band = module
      </div>
      {model && model.states.length > 0 && (
        <div style={{ color: mut, fontSize: 12, marginBottom: 4 }}>
          states (same as board):{" "}
          {model.states.map((s) => (
            <span key={s.name} style={{ marginRight: 12 }}>
              <Swatch color={s.color} />
              {s.name}
            </span>
          ))}
        </div>
      )}
      {hasMovement && (
        <div style={{ color: mut, fontSize: 12, marginBottom: 4 }}>
          lifecycle (rings):{" "}
          {(["backlog", "unstarted", "started", "done", "cancelled"] as const).map((b) => (
            <span key={b} style={{ marginRight: 12 }}>
              <Swatch color={BUCKET_COLOR[b]} />
              {BUCKET_LABEL[b]}
            </span>
          ))}
        </div>
      )}
      {model && model.lanes.length > 0 && (
        <div style={{ color: mut, fontSize: 12, marginBottom: 8 }}>
          outer band = module:{" "}
          {model.lanes.map((l) => (
            <span key={l.name} style={{ marginRight: 12 }}>
              <Swatch color={l.color} />
              {l.name}
            </span>
          ))}
        </div>
      )}
      {/* the SVG from the pure geometry, rendered client-side */}
      <div dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  );
}
