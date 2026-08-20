/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** BIP-41 M5.3 banner rules — the scope doc's sentences as assertions.
 *  The load-bearing one: UNKNOWN renders as unknown in BOTH directions —
 *  never hidden-as-current, never shown-as-update. */

import { describe, expect, it } from "vitest";
import { updateBannerDecision } from "../components/instance/update-banner-state";
import type { TUpdateCheckStatus } from "@plane/types";

const base: TUpdateCheckStatus = {
  state: "current",
  reason: null,
  checked_at: "2026-08-12T00:00:00Z",
  running_release: "v1.0.0",
  latest_release: { tag: "v1.0.0", level: "code", changelog_url: "https://x/v1.0.0" },
  source: "github",
};

describe("updateBannerDecision", () => {
  it("current renders nothing", () => {
    expect(updateBannerDecision(base)).toEqual({ kind: "hidden" });
  });

  it("update_available shows tag and changelog", () => {
    const decision = updateBannerDecision({
      ...base,
      state: "update_available",
      running_release: "v0.9.0",
      latest_release: { tag: "v1.0.0", level: "code", changelog_url: "https://x/v1.0.0" },
    });
    expect(decision).toEqual({
      kind: "update",
      tag: "v1.0.0",
      level: "code",
      manualRequired: false,
      changelogUrl: "https://x/v1.0.0",
    });
  });

  it("full-level updates say manual honestly", () => {
    const decision = updateBannerDecision({
      ...base,
      state: "update_available",
      latest_release: { tag: "v2.0.0", level: "full", changelog_url: null },
    });
    expect(decision.kind).toBe("update");
    if (decision.kind === "update") expect(decision.manualRequired).toBe(true);
  });

  it("unknown is SHOWN with its reason — never hidden as current", () => {
    const decision = updateBannerDecision({
      ...base,
      state: "unknown",
      reason: "pins file unreadable: gone",
      latest_release: null,
    });
    expect(decision).toEqual({ kind: "unknown", reason: "pins file unreadable: gone" });
  });

  it("unknown never claims an update either — both directions", () => {
    const decision = updateBannerDecision({
      ...base,
      state: "unknown",
      reason: "latest release manifest is unsigned or failed verification",
      latest_release: { tag: "v9.9.9", level: null, changelog_url: null },
    });
    // A tag is visible in the payload, but unknown must not advertise it.
    expect(decision.kind).toBe("unknown");
  });

  it("a failed or missing fetch is unknown, not silence", () => {
    expect(updateBannerDecision(null)).toEqual({
      kind: "unknown",
      reason: "update status could not be fetched",
    });
    expect(updateBannerDecision(undefined)).toEqual({
      kind: "unknown",
      reason: "update status could not be fetched",
    });
  });

  it("update_available without a tag is malformed and renders unknown", () => {
    const decision = updateBannerDecision({
      ...base,
      state: "update_available",
      latest_release: null,
      reason: null,
    });
    expect(decision).toEqual({ kind: "unknown", reason: "update status is unknown" });
  });

  it("an empty changelog url stays falsy so the banner renders no link", () => {
    // The server sends "" when the release's html_url failed origin
    // validation (unsigned response metadata, Rowan 3363 #3) — the update
    // still advertises, but there must be nothing to click.
    const decision = updateBannerDecision({
      ...base,
      state: "update_available",
      latest_release: { tag: "v2.0.0", level: "code", changelog_url: "" },
      reason: null,
    });
    expect(decision.kind).toBe("update");
    if (decision.kind === "update") {
      expect(decision.changelogUrl).toBeFalsy();
    }
  });
});
