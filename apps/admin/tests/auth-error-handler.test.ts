/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: a message entry without membership in bannerAlertErrorCodes is dead
// code — authErrorHandler returns undefined and every consumer silently skips
// (Sable RC 3029: PASSWORD_TOO_WEAK had a message but no membership in the
// shared copy, so the sign-up bounce could never reopen the password step).
// This pins the codes whose HANDLER EXISTENCE is load-bearing.
import { describe, expect, it } from "vitest";

import { EAuthErrorCodes } from "@plane/constants";
import { authErrorHandler } from "@plane/utils";

describe("authErrorHandler membership (RC 3029)", () => {
  it("returns a banner handler for PASSWORD_TOO_WEAK — the weak-password bounce depends on it", () => {
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
