/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
// plane imports
import { InstanceService } from "@plane/services";
// local imports
import { updateBannerDecision } from "./update-banner-state";
import {
  APPLY_DONE_STAGE,
  APPLY_START_STAGE,
  type TApplyStage,
  advanceApplyStage,
  applyRunVerdict,
  isIndeterminate,
  isRollingBack,
} from "@plane/utils";

const instanceService = new InstanceService();

/** The apply half (ticket 69): one button, one state machine, no retries.
 *
 *  idle → requesting → started | refused. The applier's own refusals (409
 *  already-running, 501 not configured, 502 unreachable) surface as text in
 *  the banner — the button never pretends more than the server told it.
 *  While a run is live we poll the applier status and show its outcome
 *  honestly, including a non-zero exit.
 */
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
    <span className="inline-flex min-w-64 flex-col gap-0.5 align-middle">
      <span className="text-xs flex items-center justify-between">
        <span>
          Updating to {tag} — {stage.label}…
        </span>
        <span>{indeterminate ? (isRollingBack(stage) ? "" : "progress not reported") : `${stage.pct}%`}</span>
      </span>
      {indeterminate ? null : (
        <span className="bg-layer-1 block h-1.5 w-full overflow-hidden rounded">
          <span
            className="bg-accent-primary block h-full rounded transition-all duration-700"
            style={{ width: `${stage.pct}%` }}
          />
        </span>
      )}
    </span>
  );
}

function ApplyButton({ tag }: { tag: string }) {
  const [phase, setPhase] = useState<"idle" | "requesting" | "started" | "refused">("idle");
  const [detail, setDetail] = useState<string | null>(null);

  // Verdict discriminator: the applier's own finished_at, baselined before
  // the click. Three baseline states (not-yet / confirmed-none / could-not-
  // read) — an unreadable baseline must not enter polling claiming one
  // (Sable 3962, Vex 3960). Pure logic + tests in @plane/utils apply-progress.
  const [baseline, setBaseline] = useState<number | null | "unreadable" | undefined>(undefined);
  const [sawRunning, setSawRunning] = useState(false);
  const [stage, setStage] = useState<TApplyStage>(APPLY_START_STAGE);
  // Poll-failure signal — same reasoning as the web Updates page: the apply
  // restarts the api, polls throw for minutes on small hardware, and a bar
  // frozen without a stated reason reads as a hang.
  const { data: run, error: runPollError } = useSWR(
    phase === "started" ? "BIPLANE_APPLY_RUN_STATUS" : null,
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
    // One clean reload on success — the operator never refreshes by hand.
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
      if (verdict?.started) {
        setPhase("started");
      } else {
        setPhase("refused");
        setDetail(verdict?.error ?? "the apply was refused");
      }
    } catch (e) {
      setPhase("refused");
      setDetail((e as { error?: string })?.error ?? "the apply could not be requested");
    }
  };

  if (phase === "started") {
    if (finished) {
      return succeeded ? (
        <span className="inline-flex flex-col gap-0.5 align-middle">
          <ApplyProgress tag={tag} stage={APPLY_DONE_STAGE} />
          <span className="font-medium">Update to {tag} finished — reloading…</span>
        </span>
      ) : (
        <span className="text-red-500 font-medium">
          Update failed (exit {exitCode}) — see the apply log on the host.
        </span>
      );
    }
    return (
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
  }
  if (phase === "refused") {
    return <span className="text-red-500">{detail}</span>;
  }
  return (
    <button
      type="button"
      onClick={request}
      disabled={phase === "requesting"}
      className="border-subtle bg-accent-primary rounded border px-2 py-0.5 text-white disabled:opacity-50"
    >
      {phase === "requesting" ? "Requesting…" : "Update now"}
    </button>
  );
}

/** BIP-41 M5.3: the admin update banner.
 *
 *  Render rules live in updateBannerDecision (pure, tested); this component
 *  only fetches and paints. Read-only: the status endpoint is local to the
 *  server, so this polls cheap truth — it can never trigger the outbound
 *  check. The apply action arrives with BIP-42's POST; until then the banner
 *  links the changelog and the documented upgrade steps, which is exactly
 *  M5.3's scope.
 */
export function UpdateBanner() {
  const { data, error } = useSWR("BIPLANE_UPDATE_CHECK_STATUS", () => instanceService.updateCheckStatus(), {
    refreshInterval: 5 * 60 * 1000,
    revalidateOnFocus: false,
  });

  const decision = updateBannerDecision(error ? null : data);

  if (decision.kind === "hidden") return null;

  if (decision.kind === "update") {
    return (
      <div className="border-subtle bg-accent-primary/10 text-sm flex items-center gap-2 border-b px-4 py-2">
        <span className="font-medium">Update available: {decision.tag}</span>
        {decision.manualRequired ? (
          <span>— this release changes dependencies and needs the short manual upgrade path.</span>
        ) : (
          <ApplyButton tag={decision.tag} />
        )}
        {decision.changelogUrl ? (
          <a href={decision.changelogUrl} target="_blank" rel="noreferrer" className="underline">
            What changed
          </a>
        ) : null}
      </div>
    );
  }

  // unknown — shown, never hidden: unknown must not masquerade as up to date.
  return (
    <div className="border-subtle text-xs text-secondary border-b px-4 py-1.5">
      Update status unknown — {decision.reason}
    </div>
  );
}
