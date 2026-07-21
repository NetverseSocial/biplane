// biplane Enhanced Wheel — the multi-ring sunburst, ported from the SBPM wheel
// (sbl/pm src/app.ts, the loadWheel/ring logic) and re-pointed at Plane's issue data.
//
// Layout, per ticket, as a radial slice whose angular width scales with priority:
//   inner ring  = the ticket's state as of the cutoff (24h ago) — the "change ring"
//   middle ring = the ticket's state now
//   outer band  = the module lane (a colour per module)
//   pip         = a white dot when the state moved inside the window
//   label       = leader line + "#<seq> <name>", bold + "▸" when it moved
//   centre      = ticket count + render timestamp
//
// Everything here is PURE and server-renderable: buildWheelModel() turns Plane issues
// into a geometry model, renderWheelSVG() turns that model into an <svg> string. No DOM,
// no client JS, no network — so the geometry is unit-tested against fixtures directly.

// ---- Plane issue shape (only the fields the wheel reads) ----
export type Priority = "urgent" | "high" | "medium" | "low" | "none";
export type StateGroup = "backlog" | "unstarted" | "started" | "completed" | "cancelled";

export interface PlaneIssue {
  id: string;
  sequence_id: number;
  name: string;
  priority: Priority;
  state?: string; // state id
  state_detail?: { name?: string; group?: StateGroup; color?: string };
  created_at?: string;
  updated_at?: string;
  module_ids?: string[];
  module_name?: string; // enriched by the fetch layer when modules resolve
  assignees?: string[];
}

// ---- Buckets: the four+one visual states the rings colour by ----
export type Bucket = "done" | "started" | "unstarted" | "backlog" | "cancelled";

// Priority → angular weight. Urgent slices are the widest; none/low the thinnest.
// (SBPM used P1:3/P2:2/P3:1; Plane has a 5-level scale so urgent gets its own step.)
export const PRIORITY_WIDTH: Record<Priority, number> = {
  urgent: 4,
  high: 3,
  medium: 2,
  low: 1,
  none: 1,
};

// Plane state-group → visual bucket.
export const GROUP_BUCKET: Record<StateGroup, Bucket> = {
  completed: "done",
  started: "started",
  unstarted: "unstarted",
  backlog: "backlog",
  cancelled: "cancelled",
};

// Bucket → ring colour (matches the SBPM wheel palette; cancelled added for Plane).
export const BUCKET_COLOR: Record<Bucket, string> = {
  done: "#2fbf71", // green
  started: "#4f9cf9", // blue  (in progress / building)
  unstarted: "#f2b53d", // amber (shaping / ready)
  backlog: "#6b7480", // grey  (idle / triage)
  cancelled: "#ef5350", // red
};

export const BUCKET_LABEL: Record<Bucket, string> = {
  done: "completed",
  started: "started",
  unstarted: "unstarted",
  backlog: "backlog",
  cancelled: "cancelled",
};

// For the change-ring heuristic: the bucket a ticket most likely came FROM when it
// moved inside the window but we have no activity history to prove the exact prior.
// Follows the natural lifecycle order backlog → unstarted → started → completed.
const PREDECESSOR: Record<Bucket, Bucket> = {
  done: "started",
  started: "unstarted",
  unstarted: "backlog",
  backlog: "backlog", // nothing before backlog
  cancelled: "cancelled", // cancellation has no meaningful "prior lane"
};

// Distinct, saturated hues for module lanes (ported from SBPM LANES_C).
export const LANE_COLORS = [
  "#6c4bd1",
  "#d14b8f",
  "#cf7a2e",
  "#2f8f8f",
  "#8f6f2f",
  "#3f6fd1",
  "#8f2f6f",
];

const KNOWN_GROUPS = new Set<StateGroup>([
  "backlog",
  "unstarted",
  "started",
  "completed",
  "cancelled",
]);

const esc = (s: unknown): string =>
  String(s ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!,
  );

// ---- mapping helpers (each independently testable) ----

export function priorityWidth(p: Priority | string | undefined | null): number {
  return PRIORITY_WIDTH[(p as Priority) ?? "none"] ?? 1;
}

export function defaultGroupOf(iss: PlaneIssue): StateGroup {
  const g = iss.state_detail?.group;
  return g && KNOWN_GROUPS.has(g) ? g : "backlog";
}

export function bucketOf(group: StateGroup): Bucket {
  return GROUP_BUCKET[group] ?? "backlog";
}

export function defaultLaneOf(iss: PlaneIssue): string {
  return iss.module_name ?? iss.module_ids?.[0] ?? "unassigned";
}

// The change-ring bucket for a ticket. If the ticket was last touched before the
// cutoff it is treated as stable (prev = now); if it moved inside the window we
// approximate the prior bucket as its lifecycle predecessor. An exact prior can be
// supplied out-of-band (e.g. from the Audit Ledger) via buildWheelModel's priorState.
export function prevBucket(
  nowBucket: Bucket,
  updatedAt: string | undefined,
  cutoffIso: string,
): Bucket {
  if (!updatedAt || updatedAt <= cutoffIso) return nowBucket;
  return PREDECESSOR[nowBucket];
}

// ---- the geometry model ----

export interface WheelSlice {
  id: string;
  label: string; // "#<sequence_id>"
  title: string;
  priority: Priority;
  now: Bucket;
  nowColor: string; // the state's own Plane colour (per-state, not just the 5 group buckets)
  nowName: string; // the state's display name
  prev: Bucket;
  changed: boolean;
  width: number;
  a0: number; // start angle (radians)
  a1: number; // end angle
  mid: number; // mid angle (for pip + label)
  lane: string;
  laneColor: string;
}

export interface WheelModel {
  cx: number;
  cy: number;
  rHole: number;
  rMid: number;
  r: number;
  rb: number;
  slices: WheelSlice[];
  lanes: { name: string; color: string }[];
  states: { name: string; color: string }[]; // distinct current states present, for the legend
  total: number; // sum of widths
  count: number; // number of tickets
  timestamp: string; // ISO render time
}

export interface BuildOpts {
  now?: number; // Date.now() override (tests)
  cutoffMs?: number; // window for the change ring; default 24h
  laneOf?: (i: PlaneIssue) => string;
  groupOf?: (i: PlaneIssue) => StateGroup;
  priorState?: Map<string, Bucket>; // exact 24h-ago buckets, if known
}

// Plane state colours are UNTRUSTED (a crafted value like `"><script>` would break out of
// the SVG fill attribute — stored XSS). Allow only a strict #hex or a bare colour word;
// otherwise fall back to the group-bucket colour.
export function safeColor(c: string | undefined, fallback: string): string {
  const v = String(c ?? "").trim();
  return /^#[0-9a-fA-F]{3,8}$/.test(v) || /^[a-zA-Z]{1,24}$/.test(v) ? v : fallback;
}

export function buildWheelModel(issues: PlaneIssue[], opts: BuildOpts = {}): WheelModel {
  const now = opts.now ?? Date.now();
  const groupOf = opts.groupOf ?? defaultGroupOf;
  const laneOf = opts.laneOf ?? defaultLaneOf;

  const enriched = issues
    .map((iss) => {
      const nowB = bucketOf(groupOf(iss));
      // Movement is only rendered from AUTHORITATIVE history (opts.priorState). We do NOT
      // infer a prior bucket from updated_at — a title/priority/comment edit is not a state
      // transition, and presenting an inferred move as fact was the Morrow re-gate blocker.
      // With no history supplied, prev == now → the slice is drawn as stable (no ▸/pip).
      const prevB = opts.priorState?.get(iss.id) ?? nowB;
      return {
        iss,
        lane: laneOf(iss) || "unassigned",
        now: nowB,
        // per-state colour/name from Plane (falls back to the group bucket if absent)
        nowColor: safeColor(iss.state_detail?.color, BUCKET_COLOR[nowB]),
        nowName: iss.state_detail?.name || BUCKET_LABEL[nowB],
        prev: prevB,
        width: priorityWidth(iss.priority),
      };
    })
    // group visually by lane, stable within a lane by ticket number
    .sort((a, b) => a.lane.localeCompare(b.lane) || a.iss.sequence_id - b.iss.sequence_id);

  const laneNames = [...new Set(enriched.map((e) => e.lane))];
  const laneColor: Record<string, string> = Object.fromEntries(
    laneNames.map((n, i) => [n, LANE_COLORS[i % LANE_COLORS.length]]),
  );
  const total = enriched.reduce((a, e) => a + e.width, 0) || 1;

  const slices: WheelSlice[] = [];
  let a = -Math.PI / 2; // start at 12 o'clock, sweep clockwise
  for (const e of enriched) {
    const a0 = a;
    const a1 = a + (2 * Math.PI * e.width) / total;
    const mid = (a0 + a1) / 2;
    a = a1;
    slices.push({
      id: e.iss.id,
      label: "#" + e.iss.sequence_id,
      title: e.iss.name ?? "",
      priority: e.iss.priority,
      now: e.now,
      nowColor: e.nowColor,
      nowName: e.nowName,
      prev: e.prev,
      changed: e.prev !== e.now,
      width: e.width,
      a0,
      a1,
      mid,
      lane: e.lane,
      laneColor: laneColor[e.lane]!,
    });
  }

  return {
    cx: 560,
    cy: 440,
    rHole: 78,
    rMid: 150,
    r: 215,
    rb: 235,
    slices,
    lanes: laneNames.map((n) => ({ name: n, color: laneColor[n]! })),
    states: (() => {
      const seen = new Map<string, string>();
      for (const e of enriched) if (!seen.has(e.nowName)) seen.set(e.nowName, e.nowColor);
      return [...seen].map(([name, color]) => ({ name, color }));
    })(),
    total,
    count: enriched.length,
    timestamp: new Date(now).toISOString(),
  };
}

// ---- SVG ----

// Annular-arc path between radii ri..ro over angles a0..a1 (ported verbatim from SBPM).
export function ringPath(
  cx: number,
  cy: number,
  a0: number,
  a1: number,
  ri: number,
  ro: number,
): string {
  const big = a1 - a0 > Math.PI ? 1 : 0;
  const x0 = cx + ro * Math.cos(a0),
    y0 = cy + ro * Math.sin(a0);
  const x1 = cx + ro * Math.cos(a1),
    y1 = cy + ro * Math.sin(a1);
  const xi1 = cx + ri * Math.cos(a1),
    yi1 = cy + ri * Math.sin(a1);
  const xi0 = cx + ri * Math.cos(a0),
    yi0 = cy + ri * Math.sin(a0);
  return (
    `M ${x0} ${y0} A ${ro} ${ro} 0 ${big} 1 ${x1} ${y1} ` +
    `L ${xi1} ${yi1} A ${ri} ${ri} 0 ${big} 0 ${xi0} ${yi0} Z`
  );
}

// Render the whole wheel as a self-contained <svg> element string.
// Human-friendly time (server-side, so UTC) — "Jul 20, 4:59 PM" instead of a raw ISO.
// The page header re-formats to the viewer's LOCAL time client-side.
export function friendlyTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(+d)) return iso;
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function renderWheelSVG(m: WheelModel, theme: "light" | "dark" = "dark"): string {
  const { cx, cy, rHole, rMid, r, rb } = m;
  // Theme-aware text/separator colours so labels stay readable on either background.
  const ink = theme === "light" ? "#1a1d21" : "#e8ecf1";
  const muted = theme === "light" ? "#5b6673" : "#9aa6b2";
  const sep = theme === "light" ? "#ffffff" : "#0f1216";
  const g: string[] = [];

  for (const s of m.slices) {
    // inner ring: with no authoritative movement it mirrors the CURRENT state's own Plane
    // colour (so the whole slice matches the board/Kanban); only when a real prior state is
    // known does it show the prior lifecycle-bucket colour (the change ring).
    const innerFill = s.changed ? BUCKET_COLOR[s.prev] : s.nowColor;
    g.push(
      `<path d="${ringPath(cx, cy, s.a0, s.a1, rHole, rMid)}" fill="${innerFill}" stroke="${sep}" stroke-width="1.5"/>`,
    );
    // middle ring = current state (coloured by the state's OWN colour, per-state)
    g.push(
      `<path d="${ringPath(cx, cy, s.a0, s.a1, rMid, r)}" fill="${s.nowColor}" stroke="${sep}" stroke-width="1.5">` +
        `<title>${esc(s.label)} ${esc(s.title)} — ${esc(s.priority)} · ${esc(s.nowName)}</title></path>`,
    );
    // movement pip
    if (s.changed) {
      g.push(
        `<circle cx="${cx + rMid * Math.cos(s.mid)}" cy="${cy + rMid * Math.sin(s.mid)}" r="3.2" fill="#fff"/>`,
      );
    }
    // leader line + label
    const side = Math.cos(s.mid) >= 0 ? 1 : -1;
    const lx1 = cx + (rb + 16) * Math.cos(s.mid),
      ly1 = cy + (rb + 16) * Math.sin(s.mid);
    g.push(
      `<line x1="${cx + (rb + 2) * Math.cos(s.mid)}" y1="${cy + (rb + 2) * Math.sin(s.mid)}" x2="${lx1}" y2="${ly1}" stroke="${s.nowColor}" stroke-width="1.2"/>`,
    );
    g.push(
      `<text x="${lx1 + side * 6}" y="${ly1 + 5}" font-size="16" font-weight="${s.changed ? 700 : 500}" ` +
        `fill="${ink}" text-anchor="${side > 0 ? "start" : "end"}" font-family="sans-serif">` +
        `${s.changed ? "▸ " : ""}${esc(s.label)} ${esc(s.title.slice(0, 26))}</text>`,
    );
  }

  // outer band = module lane, one segment per slice (contiguous by construction)
  for (const s of m.slices) {
    g.push(
      `<path d="${ringPath(cx, cy, s.a0, s.a1, r + 3, rb)}" fill="${s.laneColor}"><title>${esc(s.lane)}</title></path>`,
    );
  }

  // centre readout
  g.push(
    `<text x="${cx}" y="${cy + 2}" font-size="20" font-weight="600" fill="${muted}" text-anchor="middle" font-family="sans-serif">${m.count} tickets</text>`,
  );

  // viewBox cropped to the actual drawn content (was 0 0 1180 900 with ~40% dead
  // whitespace) so the wheel fills the panel and text renders larger for the same panel width.
  return `<svg id="wheel-svg" viewBox="20 150 1080 575" style="width:100%;max-width:1080px;overflow:visible">${g.join("")}</svg>`;
}

export { esc };
