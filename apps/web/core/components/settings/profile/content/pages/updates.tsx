"use client";
// biplane — Settings → Updates (John's design, 2026-08-16; his screenshots in
// docs/aria are normative). Sections in his order: current version + when it
// was updated; a newer release when one exists (with the action); automatic
// updates; our changelog, scrollable.

import { useEffect, useState } from "react";
import useSWR from "swr";
// plane imports
import { ToggleSwitch } from "@plane/ui";
// services — the WEB app's authenticated client (the shared @plane/services
// client reaches the API with no session in this app; measured user_id null)
import { InstanceUpdatesService } from "@/services/instance-updates.service";
import {
  APPLY_DONE_STAGE,
  APPLY_START_STAGE,
  type TApplyStage,
  advanceApplyStage,
  applyRunVerdict,
  isIndeterminate,
  isRollingBack,
} from "@plane/utils";

const instanceService = new InstanceUpdatesService();

// Honest, MONOTONIC stage gauge — pure logic + vitest suite in
// @plane/utils (apply-progress.ts), imported by both apps. Byte-level
// pull percent is future-changes 16.
function ApplyProgress({ tag, stage }: { tag: string; stage: TApplyStage }) {
  // No stage reported means no progress to claim (BIP-72): an applier older
  // than the stage protocol never marks, and deployments/ ships by hand, so
  // that is a real deployment rather than a hypothetical. Draw no bar and
  // state the absence — a number here would be the confident-wrong-value
  // this change removes.
  const indeterminate = isIndeterminate(stage);
  return (
    <div className="mt-1 flex max-w-md flex-col gap-1">
      <div className="text-xs text-secondary flex items-center justify-between">
        <span>
          Updating to {tag} — {stage.label}…
        </span>
        <span>{indeterminate ? (isRollingBack(stage) ? "" : "progress not reported") : `${stage.pct}%`}</span>
      </div>
      {indeterminate ? null : (
        <div className="bg-layer-1 h-2 w-full overflow-hidden rounded">
          <div
            className="bg-accent-primary h-full rounded transition-all duration-700"
            style={{ width: `${stage.pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

function NewerRelease({ tag, level }: { tag: string; level: string | null }) {
  const [phase, setPhase] = useState<"idle" | "requesting" | "started" | "refused">("idle");
  const [detail, setDetail] = useState<string | null>(null);
  // Verdict discriminator: the applier's own finished_at, baselined before
  // the click. Three baseline states (not-yet / confirmed-none / could-not-
  // read) — an unreadable baseline must not enter polling claiming one
  // (Sable 3962, Vex 3960). Pure logic + tests in @plane/utils apply-progress.
  const [baseline, setBaseline] = useState<number | null | "unreadable" | undefined>(undefined);
  const [sawRunning, setSawRunning] = useState(false);
  const [stage, setStage] = useState<TApplyStage>(APPLY_START_STAGE);
  // No catch on the fetcher: a resolved null is a "successful fetch of
  // nothing" that SWR stores, wiping last-good data during the ~60s api
  // warmup on every apply (Vex 3960, Sable 3962). On a throw, SWR keeps
  // the previous data — which is what the gauge should hold anyway.
  // `error` is the poll-failure signal: during the apply's restart stage the
  // api container itself goes down and then cold-boots — minutes on a Pi — so
  // every poll in that window throws. SWR keeps the last data (the bar holds)
  // and clears the error on the first successful poll. Rendering the error is
  // what turns "frozen at 53% for three minutes" (observed on the v1.2.9
  // apply, 2026-08-19) into a stated, expected quiet period: the statement is
  // derived from a real failed poll plus a real last stage — not a timer, not
  // an animation.
  const { data: run, error: runPollError } = useSWR(
    phase === "started" ? "BIPLANE_APPLY_RUN_STATUS_SETTINGS" : null,
    () => instanceService.applyUpdateStatus(),
    { refreshInterval: 5 * 1000, revalidateOnFocus: false }
  );
  useEffect(() => {
    if (run?.running) setSawRunning(true);
    setStage((prev: TApplyStage) => advanceApplyStage(prev, run?.log_tail));
  }, [run]);
  const runVerdict = phase === "started" ? applyRunVerdict(baseline, sawRunning, run) : { finished: false as const };
  const finished = runVerdict.finished;
  const exitCode = runVerdict.finished ? runVerdict.exitCode : undefined;
  const succeeded = finished && exitCode === 0;
  useEffect(() => {
    // The page updated the software under its own feet; making the operator
    // reload by hand was called out as bad UI the moment it shipped. One
    // clean reload once the apply reports success.
    if (!succeeded) return;
    // 2500ms was not enough to see, and the bar never reached 100 anyway: stages
    // topped out at 95 and the UI jumped straight to the success text. Show the
    // completed bar, then hold it. A completion nobody sees did not communicate.
    const timer = setTimeout(() => window.location.reload(), 4500);
    return () => clearTimeout(timer);
  }, [succeeded]);

  const request = async () => {
    setPhase("requesting");
    // Per-run reset: monotonicity and the seen-running fallback belong to
    // THIS run, not the component's lifetime (Sable 3963; future-changes 22).
    setStage(APPLY_START_STAGE);
    setSawRunning(false);
    setBaseline(undefined);
    try {
      try {
        const before = await instanceService.applyUpdateStatus();
        setBaseline(before?.last_result?.finished_at ?? null);
      } catch {
        setBaseline("unreadable");
      }
      const verdict = await instanceService.applyUpdate();
      if (verdict?.started) setPhase("started");
      else {
        setPhase("refused");
        setDetail(verdict?.error ?? "the update was refused");
      }
    } catch (e) {
      setPhase("refused");
      setDetail((e as { error?: string })?.error ?? "the update could not be requested");
    }
  };

  if (level === "full")
    return (
      <p className="text-sm text-secondary">
        {tag} is available. It changes the runtime itself, so it takes the short manual upgrade path rather than
        one-click.
      </p>
    );
  if (phase === "started")
    return finished ? (
      succeeded ? (
        <div>
          <ApplyProgress tag={tag} stage={APPLY_DONE_STAGE} />
          <p className="text-sm font-medium mt-1">Updated to {tag} — reloading…</p>
        </div>
      ) : (
        <p className="text-sm text-red-500">Update failed (exit {exitCode}) — see the apply log on the host.</p>
      )
    ) : (
      <div>
        <ApplyProgress tag={tag} stage={stage} />
        {runPollError ? (
        <p className="text-xs text-tertiary mt-1">
          The server is not answering status checks right now. The bar holds at the last reported stage and catches up
          when the server returns.
          {(stage.pct ?? 0) >= 80 ? " This is expected while the apply restarts its services, which takes a few minutes on small hardware." : ""}
        </p>
      ) : null}
      </div>
    );
  if (phase === "refused") return <p className="text-sm text-red-500">{detail}</p>;
  return (
    <button
      type="button"
      onClick={request}
      disabled={phase === "requesting"}
      className="border-subtle bg-accent-primary text-sm rounded border px-3 py-1.5 text-white disabled:opacity-50"
    >
      {phase === "requesting" ? "Requesting…" : `Update to ${tag}`}
    </button>
  );
}

export function UpdatesProfileSettings() {
  const [toggleError, setToggleError] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [pendingCustom, setPendingCustom] = useState(false);
  const [customUrl, setCustomUrl] = useState("");
  const { data: status } = useSWR(
    "BIPLANE_UPDATE_STATUS_SETTINGS",
    () => instanceService.updateCheckStatus().catch(() => null),
    {
      refreshInterval: 5 * 60 * 1000,
      revalidateOnFocus: false,
    }
  );
  const { data: auto, mutate: mutateAuto } = useSWR(
    "BIPLANE_AUTO_SETTING",
    () => instanceService.autoApplySetting().catch(() => null),
    {
      revalidateOnFocus: false,
    }
  );
  const { data: source, mutate: mutateSource } = useSWR(
    "BIPLANE_UPDATE_SOURCE",
    () => instanceService.updateSourceSetting().catch(() => null),
    {
      revalidateOnFocus: false,
    }
  );
  const { data: changelog } = useSWR("BIPLANE_OUR_CHANGELOG", () => instanceService.ourChangelog().catch(() => null), {
    revalidateOnFocus: false,
  });

  // The SERVER's answer, not this bundle's. `import.meta.env` is compiled in at
  // build time, so a tab holding an older bundle reports the version it was
  // built from for as long as it stays open — and reports it with no hedge,
  // while the check line beside it refreshes from the API. That is a display
  // that disagrees with itself, and the half a reader trusts is the wrong half.
  // `running_release` is the deployment's own `biplane_installed_version`.
  const bundleVersion = import.meta.env.VITE_BIPLANE_VERSION || null;
  const build = import.meta.env.VITE_BIPLANE_BUILD || "dev";
  const installed = status?.running_release || bundleVersion;
  // THREE states, not two (Sable 4038). `running_release` is null in four of
  // classify()'s seven branches, and two of those — no reachable release
  // source, and a latest tag that is not semver — are OUTBOUND failures. In
  // both, what this deployment is running is not in doubt at all; it is sitting
  // in `biplane_installed_version`. So a null must not be read as "the version
  // is uncertain", and collapsing it into the agrees-case would silently
  // restore the exact behaviour this fix removes, dropping the hedge at the
  // moment the display is least trustworthy.
  const serverAnswered = Boolean(status?.running_release);
  // Only ever true on evidence: the server answered AND the two disagree.
  const bundleIsStale = Boolean(serverAnswered && bundleVersion && status?.running_release !== bundleVersion);
  // Answered-and-agrees is the only state that needs no qualifier.
  const unverified = !serverAnswered && Boolean(bundleVersion);
  const flagged = status?.state === "update_available" && status.latest_release?.tag;

  if (!status)
    return (
      <div className="text-sm text-secondary">
        Loading update status… If this never resolves, either you are not an instance admin (this page is admin-only) or
        the server has no update check configured.
      </div>
    );

  return (
    <div className="flex flex-col gap-8">
      {/* current version */}
      <section>
        <h3 className="text-lg font-medium">Current version</h3>
        <p className="text-sm text-secondary mt-1">
          {installed ? `Biplane ${installed}${bundleIsStale ? "" : ` (build ${build})`}` : `Biplane dev build ${build}`}
        </p>
        {bundleIsStale ? (
          <p className="text-xs text-tertiary">
            This page is still running the {bundleVersion} interface (build {build}). Reload to load {installed}.
          </p>
        ) : null}
        {unverified ? (
          <p className="text-xs text-tertiary">
            Read from this page&apos;s own bundle — the server did not report a running version, so this
            may be out of date.
          </p>
        ) : null}
        {status.checked_at ? (
          <p className="text-xs text-tertiary">
            Last update check: {new Date(status.checked_at).toLocaleString()}
            {status.state === "current" ? " — up to date" : ""}
            {status.state === "unknown" ? ` — status unknown (${status.reason ?? "no verdict"})` : ""}
          </p>
        ) : null}
      </section>

      {/* newer release, only when one exists */}
      {flagged && status.latest_release?.tag
        ? (() => {
            // Hoisted so tsc narrows once (Vex 3892: each `!` re-access is a
            // fresh expression the truthiness guard never narrowed).
            const release = status.latest_release;
            const tag = release.tag as string;
            return (
              <section>
                <h3 className="text-lg font-medium">Newer version available</h3>
                <p className="text-sm text-secondary mt-1 mb-2">
                  {tag}
                  {release.changelog_url ? (
                    <>
                      {" · "}
                      <a className="underline" href={release.changelog_url} target="_blank" rel="noreferrer">
                        what changed
                      </a>
                    </>
                  ) : null}
                </p>
                <NewerRelease tag={tag} level={release.level} />
              </section>
            );
          })()
        : (
            <section>
              <h3 className="text-lg font-medium">Update</h3>
              <p className="text-sm text-secondary mt-1 mb-2">
                {status.state === "current" ? "You’re on the latest version." : "Update status unavailable."}
              </p>
              <button
                type="button"
                disabled
                className="border-subtle text-tertiary text-sm cursor-not-allowed rounded border px-3 py-1.5 opacity-50"
              >
                Update
              </button>
            </section>
          )}

      {/* automatic updates */}
      <section>
        <h3 className="text-lg font-medium">Automatic updates</h3>
        <p className="text-sm text-secondary mt-1 mb-3">
          When on, the hourly check installs new one-click releases by itself — at most once per release. Heavier
          releases that change the runtime always wait for the manual path.
        </p>
        <div className="flex items-center gap-3">
          <ToggleSwitch
            value={!!auto?.enabled}
            onChange={async () => {
              // A swallowed failure here leaves the operator believing
              // automatic updates are on when they are not (Vex 3892) —
              // the one setting where that belief is expensive. Say it.
              const next = !auto?.enabled;
              try {
                await instanceService.setAutoApplySetting(next);
                setToggleError(null);
              } catch (e) {
                setToggleError((e as { error?: string })?.error ?? "the setting could not be saved");
              }
              void mutateAuto();
            }}
            disabled={!auto || auto.env_forced}
          />
          <span className="text-sm">{auto?.enabled ? "On" : "Off"}</span>
          {toggleError ? <span className="text-xs text-red-500">{toggleError}</span> : null}
          {auto?.env_forced ? (
            <span className="text-xs text-tertiary">forced on by the deployment configuration</span>
          ) : null}
        </div>
      </section>

      {/* update server (John's choices: Biplane.dev | GitHub | Current server | other) */}
      <section>
        <h3 className="text-lg font-medium">Update server</h3>
        <p className="text-sm text-secondary mt-1 mb-3">
          Where the update check looks for new releases. The chosen server is tried first; the others remain as fallback
          so an outage never silently stops checks.
        </p>
        <div className="flex items-center gap-3">
          <select
            className="border-subtle bg-surface-1 text-sm rounded border px-2 py-1.5"
            value={source?.source ?? "forgejo"}
            onChange={async (e) => {
              const value = e.target.value;
              if (value === "custom") {
                setPendingCustom(true);
                return;
              }
              setPendingCustom(false);
              try {
                await instanceService.setUpdateSourceSetting(value, null);
                setSourceError(null);
              } catch (err) {
                setSourceError((err as { error?: string })?.error ?? "the setting could not be saved");
              }
              void mutateSource();
            }}
          >
            <option value="forgejo">Current server</option>
            <option value="github">GitHub</option>
            <option value="biplane_dev" disabled>
              Biplane.dev (coming soon)
            </option>
            <option value="custom">Other…</option>
          </select>
          {pendingCustom || source?.source === "custom" ? (
            <form
              className="flex items-center gap-2"
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  await instanceService.setUpdateSourceSetting("custom", customUrl);
                  setSourceError(null);
                  setPendingCustom(false);
                } catch (err) {
                  setSourceError((err as { error?: string })?.error ?? "the setting could not be saved");
                }
                void mutateSource();
              }}
            >
              <input
                className="border-subtle bg-surface-1 text-sm w-72 rounded border px-2 py-1.5"
                placeholder="https://updates.example.com/releases"
                value={customUrl}
                onChange={(e) => setCustomUrl(e.target.value)}
              />
              <button type="submit" className="border-subtle text-sm rounded border px-2 py-1.5">
                Save
              </button>
            </form>
          ) : null}
          {sourceError ? <span className="text-xs text-red-500">{sourceError}</span> : null}
        </div>
        {pendingCustom || source?.source === "custom" ? (
          <p className="text-xs text-tertiary mt-2">
            Saving a server address here is the whole authorization: this instance will fetch and trust release metadata
            from it, and updates install only from it — never a fallback. Any instance admin can set this.
          </p>
        ) : null}
      </section>

      {/* our changelog, scrollable */}
      {changelog?.markdown ? (
        <section>
          <h3 className="text-lg font-medium">Changelog</h3>
          <pre className="vertical-scrollbar border-subtle bg-layer-1 text-xs mt-2 scrollbar-sm max-h-80 overflow-y-auto rounded border p-4 leading-relaxed whitespace-pre-wrap">
            {changelog.markdown}
          </pre>
        </section>
      ) : null}
    </div>
  );
}
