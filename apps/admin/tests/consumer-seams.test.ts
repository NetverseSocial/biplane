/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: PRODUCER/CONSUMER seam guards (Morrow RC 3031/3035, Sable RC 3032/3036).
//
// Four defects in this PR shared one shape — code that is correct in its own file
// but unreachable from the live path:
//   1. set-door repairs in SetPasswordForm, which the route did not render
//   2. a membership test asserting the authErrorHandler copy nothing resolves
//   3. a review claim reasoning about that same unreached copy
//   4. override state whose setter was never called (no control produced it)
// Each looked right in isolation and in the diff. These assertions are the cheap
// mechanical counter: for a new component, WHAT IMPORTS IT; for new state, WHAT
// CALLS THE SETTER. Source-level by design — these files cannot be imported into a
// node harness, and the wrong-component wiring recurred INSIDE this PR after being
// fixed once (Sable RC 3036 asked for exactly this shape).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const readRepoFile = (relative: string) =>
  readFileSync(fileURLToPath(new URL(`../../../${relative}`, import.meta.url)), "utf8");

describe("set-password route renders its own form (RC 3031)", () => {
  const page = () => readRepoFile("apps/web/app/(all)/accounts/set-password/page.tsx");

  it("imports and renders SetPasswordForm", () => {
    const source = page();
    expect(source).toContain("SetPasswordForm");
    expect(source).toMatch(/<SetPasswordForm\s*\/>/);
  });

  it("does NOT render ResetPasswordForm — it has no uidb64/token and would post undefined/undefined", () => {
    expect(page()).not.toMatch(/<ResetPasswordForm\s*\/>/);
  });
});

describe("weak-password override has a producer on every door (RC 3035)", () => {
  const doors: [string, string][] = [
    ["sign-up", "apps/web/core/components/account/auth-forms/password.tsx"],
    ["set-password", "apps/web/core/components/account/auth-forms/set-password.tsx"],
    ["reset-password", "apps/web/core/components/account/auth-forms/reset-password.tsx"],
    ["profile change", "apps/web/core/components/settings/profile/content/pages/security.tsx"],
  ];

  it.each(doors)("%s door: setAcceptWeakPassword is actually called", (_door, path) => {
    const source = readRepoFile(path);
    expect(source).toContain("acceptWeakPassword");
    // A setter that appears ONLY on its useState line is dead state.
    const setterCalls = source.match(/setAcceptWeakPassword\(/g) ?? [];
    expect(setterCalls.length).toBeGreaterThan(0);
  });

  it("onboarding door: the parent passes a producer to the child control", () => {
    const parent = readRepoFile("apps/web/core/components/onboarding/steps/profile/root.tsx");
    expect(parent).toContain("onAcceptWeakPasswordChange={setAcceptWeakPassword}");
    const child = readRepoFile("apps/web/core/components/onboarding/steps/profile/set-password.tsx");
    expect(child).toContain("onAcceptWeakPasswordChange");
    expect(child).toContain("Use this password anyway");
  });

  it("onboarding submit does not advance the step when the submit failed", () => {
    const parent = readRepoFile("apps/web/core/components/onboarding/steps/profile/root.tsx");
    // A swallowed rejection plus an unconditional advance leaves the account with no
    // password and no way back (Sable RC 3036 composition).
    expect(parent).toMatch(/if \(!succeeded\) return;/);
  });
});
