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
