/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The banner's render decision as a pure function (BIP-41, M5.3).
 *
 *  The rules the scope doc pins, kept out of JSX so they are testable:
 *  - "update_available" is the ONLY state that advertises an update.
 *  - "unknown" renders as unknown IN BOTH DIRECTIONS — it never hides as if
 *    current, and it never claims an update. The reason is shown to the
 *    operator, because every unknown carries one.
 *  - "current" (and only "current") renders nothing.
 *  - full-level updates say honestly that manual steps are required; the
 *    banner never pretends a one-click path it does not have.
 *  - A missing/failed status fetch is UNKNOWN, not silence — silence is the
 *    invisible-degrade shape.
 */

import type { TUpdateCheckStatus } from "@plane/types";

export type TUpdateBannerDecision =
  | { kind: "hidden" }
  | {
      kind: "update";
      tag: string;
      level: string | null;
      manualRequired: boolean;
      changelogUrl: string | null;
    }
  | { kind: "unknown"; reason: string };

export function updateBannerDecision(status: TUpdateCheckStatus | null | undefined): TUpdateBannerDecision {
  if (!status) {
    return { kind: "unknown", reason: "update status could not be fetched" };
  }
  if (status.state === "current") {
    return { kind: "hidden" };
  }
  if (status.state === "update_available" && status.latest_release?.tag) {
    return {
      kind: "update",
      tag: status.latest_release.tag,
      level: status.latest_release.level,
      manualRequired: status.latest_release.level === "full",
      changelogUrl: status.latest_release.changelog_url,
    };
  }
  // Everything else — declared unknown, malformed payload, update_available
  // with no tag — is unknown, with the server's reason when it gave one.
  return {
    kind: "unknown",
    reason: status.reason ?? "update status is unknown",
  };
}
