/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** BIP-41 update-check status — GET /api/instances/updates/status/.
 *  Mirrors plane/updates/classify.status_payload() exactly; the backend shape
 *  is the contract, this type only names it. */

export type TUpdateCheckState = "current" | "update_available" | "unknown";

export interface TUpdateCheckLatestRelease {
  tag: string | null;
  /** code | data | full — null until a completed check supplies it. */
  level: string | null;
  changelog_url: string | null;
}

export interface TUpdateCheckStatus {
  state: TUpdateCheckState;
  /** Operator-readable refusal reason when state is "unknown"; null otherwise. */
  reason: string | null;
  checked_at: string | null;
  running_release: string | null;
  latest_release: TUpdateCheckLatestRelease | null;
  /** Which forge answered ("forgejo" | "github"); null when none did. */
  source: string | null;
}
