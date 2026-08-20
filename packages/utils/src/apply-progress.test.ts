/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import {
  APPLY_ROLLBACK_STAGE,
  APPLY_START_STAGE,
  advanceApplyStage,
  applyRunVerdict,
  isIndeterminate,
  isRollingBack,
} from "./apply-progress";

describe("advanceApplyStage — only the applier's own marks move the gauge", () => {
  const MARK = (name: string) => `__BIPLANE_STAGE__ ${name}`;

  it("THE DEFECT (BIP-72): the applier's own failure message advances nothing", () => {
    // Live, not hypothetical. apply-service runs the applier with stderr merged
    // into apply.log, and the shipped applier prints this exact line when a
    // migration fails — it contains "migrat", so the old recogniser advanced to
    // "Running migrations, 55%" ON THE LINE ANNOUNCING THE FAILURE, and because
    // advance is monotonic the misread latched. Found by Sable.
    const failure = [
      "Biplane update failed: migration did not complete.",
      "Prior config remains active at /srv/biplane/.env; database backup: /srv/b/database.dump",
      "Operator action is required before restarting services.",
    ].join("\n");
    expect(advanceApplyStage(APPLY_START_STAGE, failure)).toEqual(APPLY_START_STAGE);
  });

  it("the recovery lines that pinned the bar at 95% claim nothing now", () => {
    // Sable's sweep found eighteen shipped messages that moved the gauge, most
    // of them failure/recovery. "probe not ready" scored Verifying 95% and,
    // being monotonic, pinned there through the entire rollback.
    const recovery = [
      "probe not ready: http://forge.test/api/instances/ (curl exit 22)",
      "RECOVERY REQUIRED: saved pins could not be restored. Inspect /srv/b/config.env",
    ].join("\n");
    expect(advanceApplyStage(APPLY_START_STAGE, recovery)).toEqual(APPLY_START_STAGE);
  });

  it("a marked line advances, and the mark must be the WHOLE line", () => {
    expect(advanceApplyStage(APPLY_START_STAGE, MARK("pull")).pct).toBe(45);
    const quoted = `Biplane update refused: unexpected output "${MARK("verify")}" from helper`;
    expect(advanceApplyStage(APPLY_START_STAGE, quoted)).toEqual(APPLY_START_STAGE);
  });

  it("no stage reported is INDETERMINATE, not five percent", () => {
    expect(APPLY_START_STAGE.pct).toBeNull();
    expect(isIndeterminate(APPLY_START_STAGE)).toBe(true);
    // What an applier older than this protocol produces: output, no marks.
    // deployments/ ships by hand, so this is a real deployment.
    const old = advanceApplyStage(APPLY_START_STAGE, "Pulling biplane-backend\nRecreate api");
    expect(isIndeterminate(old)).toBe(true);
  });

  it("ROLLBACK leaves the forward scale and is terminal", () => {
    // Rolling back is not a later stage of success. Without this the gauge held
    // "Verifying 95%" for the whole rollback — stale rather than fabricated,
    // but still wrong when the operator most needs it (Sable 3988).
    const verifying = advanceApplyStage(APPLY_START_STAGE, MARK("verify"));
    expect(verifying.pct).toBe(95);
    const rolling = advanceApplyStage(verifying, MARK("rollback"));
    expect(rolling).toEqual(APPLY_ROLLBACK_STAGE);
    expect(isRollingBack(rolling)).toBe(true);
    expect(isIndeterminate(rolling)).toBe(true);
    // Terminal: no later forward mark may put it back on the scale.
    expect(advanceApplyStage(rolling, MARK("verify"))).toEqual(APPLY_ROLLBACK_STAGE);
    // And a rollback anywhere in one tail outranks forward marks in that tail.
    expect(advanceApplyStage(APPLY_START_STAGE, [MARK("rollback"), MARK("verify")].join("\n"))).toEqual(
      APPLY_ROLLBACK_STAGE
    );
  });

  it("monotonic: a failed/null poll never lowers the stage", () => {
    const restarting = advanceApplyStage(APPLY_START_STAGE, MARK("restart"));
    expect(restarting.pct).toBe(80);
    // The api restart kills the status endpoint for ~a minute; SWR data goes
    // null. The gauge must hold, not fall back to indeterminate.
    expect(advanceApplyStage(restarting, null)).toEqual(restarting);
    expect(advanceApplyStage(restarting, undefined)).toEqual(restarting);
  });

  it("monotonic: an earlier mark still in the tail cannot drag the gauge back", () => {
    const tail = [MARK("backup"), MARK("pull"), MARK("verify"), MARK("backup")].join("\n");
    expect(advanceApplyStage(APPLY_START_STAGE, tail).pct).toBe(95);
  });

  it("a stage name from a NEWER applier is ignored, never guessed at", () => {
    const afterPull = advanceApplyStage(APPLY_START_STAGE, MARK("pull"));
    expect(advanceApplyStage(afterPull, MARK("quiescing-fleet")).pct).toBe(45);
  });
});

describe("applyRunVerdict — run identity, unknown stays unknown", () => {
  const finishedRun = { running: false, last_result: { exit_code: 1, finished_at: 1001.25 } };

  it("a run that finishes before the first poll still yields its verdict", () => {
    // Sable's bug (3958), pinned: baseline differs, running never observed.
    expect(applyRunVerdict(998.5, false, finishedRun)).toEqual({ finished: true, exitCode: 1 });
  });

  it("the previous run's result never masquerades as this run's", () => {
    expect(applyRunVerdict(1001.25, false, finishedRun)).toEqual({ finished: false });
    expect(applyRunVerdict(1001.25, true, { running: true, last_result: finishedRun.last_result })).toEqual({
      finished: false,
    });
  });

  it("an unreadable baseline stays pending until the run was seen running", () => {
    expect(applyRunVerdict("unreadable", false, finishedRun)).toEqual({ finished: false });
    expect(applyRunVerdict("unreadable", true, finishedRun)).toEqual({ finished: true, exitCode: 1 });
  });

  it("no baseline taken yet renders nothing, in either direction", () => {
    expect(applyRunVerdict(undefined, true, finishedRun)).toEqual({ finished: false });
    expect(applyRunVerdict(null, false, { running: false, last_result: null })).toEqual({ finished: false });
  });
});
