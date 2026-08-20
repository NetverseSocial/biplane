/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The apply gauge and verdict as pure functions, shared by the admin
 *  update banner and the board's Settings → Updates page (one copy imported
 *  twice — the byte-identical duplication left only the admin copy tested,
 *  and the untested twin was the demoed surface: Sable 3963 / Vex 12319,
 *  future-changes 21).
 *
 *  - The gauge is MONOTONIC within a run: it renders the highest stage the
 *    applier's log has shown, and a failed poll never lowers it. One
 *    property closes three defects (Vex, review 3960): the status endpoint
 *    dying for ~a minute while the api restarts, a success-path line
 *    matching an early stage's pattern, and early lines scrolling out of
 *    the 40-line tail window.
 *  - The verdict keys on run IDENTITY: only a finished_at different from
 *    the pre-click baseline is this run's outcome. An unreadable baseline
 *    stays pending until the run has been SEEN running — unknown stays
 *    unknown, in both directions.
 */

/** A stage with `pct: null` is INDETERMINATE — something is happening but
 *  nothing has reported where it is. That is not zero and not five percent;
 *  it is the absence of a progress claim, and it renders as one. */
export type TApplyStage = { label: string; pct: number | null };

export const APPLY_START_STAGE: TApplyStage = { label: "Working", pct: null };

/** Rolling back is NOT a later stage of success, so it carries no percentage:
 *  it leaves the forward scale entirely (Sable 3988). Without this the gauge
 *  sat at "Verifying 95%" for the whole rollback — stale rather than
 *  fabricated, but still wrong at the moment the operator most needs it. */
export const APPLY_ROLLBACK_STAGE: TApplyStage = { label: "Rolling back", pct: null };

/** The applier marks its own stage boundaries; nothing else in the log is a
 *  progress claim (BIP-72). The previous recogniser pattern-matched EVERY line
 *  of the tail against broad marks, and apply-service runs the applier with
 *  stderr merged into apply.log — so error text was read as progress. Not
 *  latent: a sweep of main found EIGHTEEN shipped messages that moved the
 *  gauge, most of them failure and recovery lines. "Biplane update failed:
 *  migration did not complete." contains "migrat", so the bar advanced to
 *  "Running migrations, 55%" on the line announcing the failure, and because
 *  advance is monotonic the misread LATCHED rather than passing.
 *
 *  Anchored WHOLE-LINE: an error quoting the sentinel is talking ABOUT
 *  progress, not reporting it. */
const STAGE_MARK = /^__BIPLANE_STAGE__ ([a-z][a-z-]*)$/;

/** KNOWN LIMIT, recorded rather than hidden (Sable 3991): stickiness protects a
 *  state already REACHED. If the rollback mark scrolls out of the 40-line tail
 *  window before any successful poll observes it — plausible, since the api is
 *  restarting during an unwind — the gauge never enters rollback and holds its
 *  last forward stage. That is the old stale-forward-stage failure rather than a
 *  new fabrication, and it is inherent to reading a tail rather than a stream;
 *  it is the one path where honest-UNKNOWN is not total. */
const ROLLBACK_MARK = "rollback";

const APPLY_STAGES: Record<string, TApplyStage> = {
  backup: { label: "Backing up", pct: 15 },
  pull: { label: "Pulling images", pct: 45 },
  // Pulling four images is the longest silent stretch of an apply — measured at
  // ~95s on the production board, during which nothing moved and the operator
  // could not tell working from wedged. These marks are emitted AFTER each
  // image completes, so they report work DONE rather than time passed: a
  // heartbeat driven by a clock outlives the work it claims to describe, and a
  // spinner driven by nothing outlives everything. Named for the images because
  // the mark grammar is [a-z][a-z-]* and carries no digits.
  "pull-backend": { label: "Pulled 1 of 4 images", pct: 47 },
  "pull-web": { label: "Pulled 2 of 4 images", pct: 49 },
  "pull-admin": { label: "Pulled 3 of 4 images", pct: 51 },
  "pull-space": { label: "Pulled 4 of 4 images", pct: 53 },
  migrate: { label: "Running migrations", pct: 55 },
  restart: { label: "Restarting services", pct: 80 },
  verify: { label: "Verifying", pct: 95 },
};

/** The bar never reached 100%: stages topped out at 95 (verify) and the UI
 *  jumped straight from there to the success text, so an operator never saw the
 *  thing complete. This is the finished state — shown briefly BEFORE the page
 *  reloads, because a completion you cannot see did not communicate anything. */
export const APPLY_DONE_STAGE: TApplyStage = { label: "Done", pct: 100 };

export function advanceApplyStage(prev: TApplyStage, tail: string | null | undefined): TApplyStage {
  // Rollback is TERMINAL for the gauge: once the applier says it is unwinding,
  // no later forward mark may put the bar back on the progress scale.
  if (isRollingBack(prev)) return prev;
  let best = prev;
  let rollingBack = false;
  if (!tail) return best;
  for (const line of tail.split("\n")) {
    const marked = STAGE_MARK.exec(line.trim());
    if (!marked) continue;
    if (marked[1] === ROLLBACK_MARK) {
      rollingBack = true;
      continue;
    }
    const stage = APPLY_STAGES[marked[1]];
    // An unknown stage name comes from an applier NEWER than this UI. Ignoring
    // it holds the last known position rather than inventing one — the same
    // reason unknown never becomes a number anywhere else here.
    if (!stage) continue;
    if (best.pct === null || (stage.pct as number) > best.pct) best = stage;
  }
  // Monotonicity protects against a MISREAD dragging the bar backwards; it was
  // never meant to outrank a report that the run is unwinding. A rollback in
  // this tail therefore wins over any forward mark in it, whatever the order.
  return rollingBack ? APPLY_ROLLBACK_STAGE : best;
}

/** No stage reported at all — either the run just began, or the installed
 *  applier predates the stage protocol and never will. The caller must not
 *  render a percentage for it. */
export function isIndeterminate(stage: TApplyStage): boolean {
  return stage.pct === null;
}

/** Compares the LABEL rather than `pct`, deliberately: the start stage is also
 *  `pct: null`, so simplifying this to `pct === null` would make every run look
 *  like a rollback and the gauge would never advance at all. Both sides read the
 *  same constant, so a relabel cannot desynchronise them (Sable 3991 probed the
 *  `pct === null` simplification rather than filing the smell: 5 of 12 tests
 *  red). */
export function isRollingBack(stage: TApplyStage): boolean {
  return stage.label === APPLY_ROLLBACK_STAGE.label;
}

export type TApplyRunView =
  | { running?: boolean; last_result?: { exit_code: number; finished_at: number } | null; log_tail?: string }
  | null
  | undefined;

export type TApplyBaseline = number | null | "unreadable" | undefined;

export function applyRunVerdict(
  baseline: TApplyBaseline,
  sawRunning: boolean,
  run: TApplyRunView
): { finished: false } | { finished: true; exitCode: number } {
  if (baseline === undefined || !run || run.running) return { finished: false };
  const last = run.last_result;
  if (last == null) return { finished: false };
  if (baseline === "unreadable") {
    // The pre-click read failed, so a previous run's result is
    // indistinguishable from this one's by identity — fall back to
    // requiring the run to have been observed running.
    return sawRunning ? { finished: true, exitCode: last.exit_code } : { finished: false };
  }
  if (last.finished_at === baseline) return { finished: false };
  return { finished: true, exitCode: last.exit_code };
}
