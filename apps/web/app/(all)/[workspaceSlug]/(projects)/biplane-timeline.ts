// biplane Traveler View — the fold.
// Turns the Audit Ledger's flat AuditEvent[] into per-item (or per-module) "medical
// record" timelines: filed → commented → state changes → done, ordered by seq. This is
// PURE — no I/O — so it is unit-tested directly against fixtures shaped like the ledger's
// GET /events emits. Every fetch/enrichment lives behind the transport in ledger-client.ts.

// Mirror of the audit-ledger's AuditEvent (src/ledger.ts). Kept as a local copy so this
// module has ZERO dependencies and can be tested in isolation.
export interface AuditEvent {
  seq: number;
  ts: string; // when the ledger recorded it (ISO-8601 UTC)
  delivery: string; // Plane's X-Plane-Delivery dedup id
  event: string; // X-Plane-Event, e.g. "issue", "issue_comment"
  action: string; // "create" | "update" | "delete" | ...
  actor: string; // best-effort actor id computed at ingest
  workspace: string; // workspace_id
  signature_ok: boolean; // did the HMAC verify at ingest
  payload: Record<string, unknown>; // full Plane webhook body
}

export type GroupKind = "item" | "module";

// One dot on the vertical timeline.
export interface TimelineEntry {
  seq: number;
  ts: string;
  actor: string; // display name if we could find one, else the actor id
  kind: string; // "filed" | "comment" | "state" | "done" | "update" | "deleted" | ...
  label: string; // human line, e.g. "Filed", "State → In Progress"
  signature_ok: boolean; // drives the verified checkmark in the UI
  event: string;
  action: string;
  itemId: string;
  module: string;
}

// One grouped record — the lifecycle of a single work item, or of a whole module.
export interface TimelineGroup {
  key: string; // itemId or module name (the group id)
  groupBy: GroupKind;
  itemId?: string; // set when groupBy === "item"
  module: string; // "" if the item has no discernible module
  name?: string; // human name, filled by Plane enrichment when available
  entries: TimelineEntry[]; // ordered by seq ascending
  first_seq: number;
  last_seq: number;
  latest_state: string; // last state name we observed, "" if none
  done: boolean; // has it reached a terminal/done state
  verified: boolean; // every entry's signature verified at ingest
  actors: string[]; // distinct actors that touched it
}

const DONE_STATES = /^(done|completed?|closed|cancell?ed|resolved)$/i;

// --- extraction helpers (best-effort against Plane's webhook shape) ---
// Plane webhook body: { event, action, workspace_id, data:{...}, activity:{field,old_value,new_value,actor} }

function data(e: AuditEvent): any {
  return (e.payload as any)?.data ?? {};
}
function activity(e: AuditEvent): any {
  return (e.payload as any)?.activity ?? {};
}

// Which work item does this event belong to? For comments/sub-resources the parent issue
// id is what we group on; for the issue itself it's data.id.
export function itemIdOf(e: AuditEvent): string {
  const d = data(e);
  const p: any = e.payload ?? {};
  if (typeof e.event === "string" && e.event.includes("comment")) {
    return String(d.issue ?? d.issue_id ?? d.issue_detail?.id ?? d.id ?? p.id ?? "");
  }
  return String(d.id ?? p.id ?? "");
}

// How "module" is represented on a Plane issue is NOT nailed down (see README follow-up).
// We probe, in order: an explicit module relation on data, a module_ids/modules array, or a
// label following a "module:<name>" convention. First hit wins; "" if none.
export function moduleOf(e: AuditEvent): string {
  const d = data(e);
  const m = d.module ?? d.module_detail;
  if (m) return String(typeof m === "object" ? (m.name ?? m.id ?? "") : m);
  const ids = d.module_ids ?? d.modules;
  if (Array.isArray(ids) && ids.length) {
    const first = ids[0];
    return String(typeof first === "object" ? (first.name ?? first.id ?? "") : first);
  }
  const labels = d.labels ?? d.label_details ?? [];
  if (Array.isArray(labels)) {
    for (const l of labels) {
      const name = typeof l === "object" ? String(l?.name ?? "") : String(l);
      const mm = /^module[:/]\s*(.+)$/i.exec(name);
      if (mm) return mm[1]!.trim();
    }
  }
  return "";
}

// Prefer a human display name for the actor; fall back to the id the ledger computed.
export function actorOf(e: AuditEvent): string {
  const a = activity(e).actor;
  if (a && typeof a === "object") {
    const name = a.display_name ?? a.email ?? a.id;
    if (name) return String(name);
  }
  return e.actor || "unknown";
}

// Classify (event, action) — plus the update's activity field — into a kind + human label.
export function classify(e: AuditEvent): { kind: string; label: string; state: string } {
  const ev = String(e.event ?? "");
  const action = String(e.action ?? "").toLowerCase();
  // Plane's real webhook vocabulary is PAST tense ("created"/"updated"/"deleted");
  // accept both tenses so the timeline doesn't silently misclassify every event.
  const isCreate = action === "create" || action === "created";
  const isUpdate = action === "update" || action === "updated";
  const isDelete = action === "delete" || action === "deleted";
  const act = activity(e);

  if (ev.includes("comment")) {
    if (isDelete) return { kind: "comment-removed", label: "Comment removed", state: "" };
    return { kind: "comment", label: "Commented", state: "" };
  }

  if (ev === "issue" || ev === "" || ev === "unknown") {
    if (isCreate) return { kind: "filed", label: "Filed", state: "" };
    if (isDelete) return { kind: "deleted", label: "Deleted", state: "" };
    if (isUpdate) {
      const field = act.field ? String(act.field) : "";
      if (field === "state") {
        const state = String(act.new_value ?? "").trim();
        const done = DONE_STATES.test(state);
        return { kind: done ? "done" : "state", label: `State → ${state || "changed"}`, state };
      }
      if (field) return { kind: "update", label: `Updated ${field}`, state: "" };
      // No activity field: infer from data.state so a done transition is still caught.
      const st = data(e).state;
      const stateName = st && typeof st === "object" ? String(st.name ?? "") : st ? String(st) : "";
      if (stateName) {
        const done = DONE_STATES.test(stateName.trim());
        return { kind: done ? "done" : "update", label: done ? `State → ${stateName}` : "Updated", state: stateName };
      }
      return { kind: "update", label: "Updated", state: "" };
    }
  }

  return { kind: `${ev}:${action}`, label: `${ev} ${action}`.trim(), state: "" };
}

// One AuditEvent → one TimelineEntry.
export function toEntry(e: AuditEvent): TimelineEntry {
  const c = classify(e);
  return {
    seq: e.seq,
    ts: e.ts,
    actor: actorOf(e),
    kind: c.kind,
    label: c.label,
    signature_ok: !!e.signature_ok,
    event: String(e.event ?? ""),
    action: String(e.action ?? ""),
    itemId: itemIdOf(e),
    module: moduleOf(e),
  };
}

// THE FOLD. Flat AuditEvent[] → grouped, ordered timelines.
// groupBy "item": one group per work item (default). groupBy "module": one group per module,
// aggregating every item's events into a single module lifetime. Events with no groupable id
// are dropped from that grouping (they cannot be placed on any record).
export function foldTimelines(events: AuditEvent[], groupBy: GroupKind = "item"): TimelineGroup[] {
  // Ingest order is authoritative; sort defensively by seq (ts is a tiebreak).
  const sorted = [...events].sort((a, b) => a.seq - b.seq || String(a.ts).localeCompare(String(b.ts)));

  // Resolve each item's module ONCE from wherever it appears. Plane update/comment webhooks
  // often omit labels, so an item's module is usually only stated on its create event — but the
  // whole record still belongs to that module. First non-empty module seen for an item wins.
  const itemModule = new Map<string, string>();
  for (const e of sorted) {
    const id = itemIdOf(e);
    if (!id) continue;
    const m = moduleOf(e);
    if (m && !itemModule.get(id)) itemModule.set(id, m);
  }
  const moduleForItem = (id: string, own: string) => own || itemModule.get(id) || "";

  const groups = new Map<string, TimelineGroup>();
  for (const e of sorted) {
    const entry = toEntry(e);
    entry.module = moduleForItem(entry.itemId, entry.module); // backfill from the item's record
    const key = groupBy === "module" ? entry.module : entry.itemId;
    if (!key) continue; // ungroupable under this mode

    let g = groups.get(key);
    if (!g) {
      g = {
        key,
        groupBy,
        itemId: groupBy === "item" ? key : undefined,
        module: groupBy === "module" ? key : entry.module,
        entries: [],
        first_seq: entry.seq,
        last_seq: entry.seq,
        latest_state: "",
        done: false,
        verified: true,
        actors: [],
      };
      groups.set(key, g);
    }

    g.entries.push(entry);
    g.first_seq = Math.min(g.first_seq, entry.seq);
    g.last_seq = Math.max(g.last_seq, entry.seq);
    if (groupBy === "item" && entry.module && !g.module) g.module = entry.module;
    if (!entry.signature_ok) g.verified = false;
    if (entry.kind === "done") g.done = true;
    if (entry.kind === "state" || entry.kind === "done") {
      const m = /State → (.+)$/.exec(entry.label);
      if (m) g.latest_state = m[1]!;
    }
    if (entry.actor && !g.actors.includes(entry.actor)) g.actors.push(entry.actor);
  }

  const out = [...groups.values()];
  for (const g of out) g.entries.sort((a, b) => a.seq - b.seq || String(a.ts).localeCompare(String(b.ts)));
  // Most-recently-active record first (nice default for a selector).
  out.sort((a, b) => b.last_seq - a.last_seq);
  return out;
}

// Convenience: fold, then pick one group (used by GET /api/timeline?item= / ?module=).
export function timelineFor(
  events: AuditEvent[],
  selector: { item?: string; module?: string },
): TimelineGroup | undefined {
  if (selector.module !== undefined) {
    return foldTimelines(events, "module").find((g) => g.key === selector.module);
  }
  return foldTimelines(events, "item").find((g) => g.key === selector.item);
}
