// biplane — deploy-time config, served as a static file next to the app (/biplane-config.json,
// lives in the web root; edit it on the server and reload — no rebuild). Controls the
// resource-protection switches: whether the board auto-refreshes at all, and how often each
// biplane surface refetches. Missing file or missing keys fall back to these defaults.
export interface BiplaneConfig {
  boardAutoRefresh: boolean; // background-refetch the project board so others' changes appear
  boardRefreshMs: number;
  wheelRefreshMs: number;
  travelerRefreshMs: number;
}

const DEFAULTS: BiplaneConfig = {
  boardAutoRefresh: true,
  boardRefreshMs: 5000,
  wheelRefreshMs: 5000,
  travelerRefreshMs: 5000,
};

// Change signal: every Plane mutation lands in the audit ledger as a webhook, bumping its
// last_seq. Polling THIS (one tiny same-origin request) instead of blindly refetching means
// idle screens do zero work and zero redraws. Each consumer gets its own instance (its own
// last-seen seq) so one surface consuming a change doesn't starve the others.
// If the ledger is unreachable, degrade to a blind refetch every `fallbackEvery` ticks.
export function ledgerSignal(fallbackEvery = 6): () => Promise<boolean> {
  let last: number | null = null;
  let misses = 0;
  return async () => {
    try {
      const r = await fetch("/biplane-ledger/health", { credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      const j = await r.json();
      const seq = typeof j?.last_seq === "number" ? j.last_seq : null;
      if (seq === null) throw new Error("no last_seq");
      misses = 0;
      if (last === null) {
        last = seq; // baseline — the surface just did its own initial fetch
        return false;
      }
      if (seq !== last) {
        last = seq;
        return true;
      }
      return false;
    } catch {
      misses += 1;
      if (misses >= fallbackEvery) {
        misses = 0;
        return true;
      }
      return false;
    }
  };
}

let cached: Promise<BiplaneConfig> | null = null;

export function biplaneConfig(): Promise<BiplaneConfig> {
  if (!cached) {
    cached = fetch("/biplane-config.json", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}))
      .then((j) => ({ ...DEFAULTS, ...(j && typeof j === "object" ? j : {}) }));
  }
  return cached;
}
