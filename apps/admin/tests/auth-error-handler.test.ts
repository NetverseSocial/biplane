/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: a message entry without membership in the banner list is dead code —
// authErrorHandler returns undefined and every caller silently skips its branch.
//
// TWO implementations of authErrorHandler exist (Sable RC 3029 → 3032):
//   packages/utils/src/auth.ts                 — shared copy, imported by packages
//   apps/web/helpers/authentication.helper.tsx — app-local copy, the one the web
//                                                sign-up flow actually resolves
// FOLLOW-UP TICKET (task #43 in the 7of9 tracker): collapse the duplication —
// FOUR copies exist (packages/utils, apps/web/helpers, apps/space/helpers,
// apps/admin auth-helpers); space/admin carry no PASSWORD_TOO_WEAK, which may be
// correct per-copy. TRIGGER, per Sable RC 3033: the NEXT change to EITHER handler
// collapses them and deletes the source-level half of this test. Without a trigger
// a ticket becomes never, and this bridge outlives everyone who remembers why.
//
// The FIRST version of this test only guarded the shared copy — the one with no
// consumer on the production path — so deleting the entry from the app-local list
// would have kept it green. It now asserts BOTH, and the app-local one is asserted
// at the SOURCE level because it cannot be imported into this (node) harness.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { EAuthErrorCodes } from "@plane/constants";
import { authErrorHandler } from "@plane/utils";

const readRepoFile = (relative: string) =>
  readFileSync(fileURLToPath(new URL(`../../../${relative}`, import.meta.url)), "utf8");

// The banner-membership array of the app-local helper, as source text.
const webHelperBannerList = () => {
  const source = readRepoFile("apps/web/helpers/authentication.helper.tsx");
  const start = source.indexOf("const bannerAlertErrorCodes = [");
  expect(start, "app-local helper must still declare bannerAlertErrorCodes").toBeGreaterThan(-1);
  return source.slice(start, source.indexOf("];", start));
};

describe("authErrorHandler membership — shared copy (runtime)", () => {
  it("returns a banner handler for PASSWORD_TOO_WEAK", () => {
    const handler = authErrorHandler(EAuthErrorCodes.PASSWORD_TOO_WEAK);
    expect(handler).toBeTruthy();
    expect(handler?.code).toBe(EAuthErrorCodes.PASSWORD_TOO_WEAK);
  });

  it("returns handlers for every code the auth flows branch on", () => {
    for (const code of [
      EAuthErrorCodes.AUTHENTICATION_FAILED_SIGN_UP,
      EAuthErrorCodes.AUTHENTICATION_FAILED_SIGN_IN,
      EAuthErrorCodes.USER_ALREADY_EXIST,
      EAuthErrorCodes.USER_DOES_NOT_EXIST,
    ]) {
      expect(authErrorHandler(code), String(code)).toBeTruthy();
    }
  });
});

describe("authErrorHandler membership — app-local copy the web flow resolves (source)", () => {
  it("lists PASSWORD_TOO_WEAK — the sign-up weak-password bounce depends on it", () => {
    // Removing this line breaks the production Password1! flow; this test is the
    // only automated guard, since no server-side route test can observe it.
    expect(webHelperBannerList()).toContain("PASSWORD_TOO_WEAK");
  });

  it("lists the sign-up name-validation codes added with their messages", () => {
    const list = webHelperBannerList();
    expect(list).toContain("REQUIRED_FIRST_NAME_SIGN_UP");
    expect(list).toContain("INVALID_NAME_SIGN_UP");
  });
});
