/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: PRODUCER/CONSUMER seam guards (Morrow RC 3031/3035/3038, Sable RC 3032/3036).
//
// Five defects in this PR shared one shape — code correct in its own file but
// unreachable from the live path: set-door repairs in a component nothing rendered;
// a membership test on the authErrorHandler copy nothing resolves; a review claim
// about that same copy; override state whose setter was never called; and then THIS
// FILE'S first version, which asserted that NAMES appear rather than that the
// load-bearing EXPRESSIONS do — Morrow RC 3038 killed it with four mutations that
// each left it 8/8 green. Presence-of-identifier is not a guard; every assertion
// below pins a complete expression, and each is answerable to a named mutation.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const readRepoFile = (relative: string) =>
  readFileSync(fileURLToPath(new URL(`../../../${relative}`, import.meta.url)), "utf8");

// Collapse whitespace so assertions survive formatter line-wrapping but still
// require the whole expression, not just its identifiers.
const flat = (relative: string) => readRepoFile(relative).replace(/\s+/g, " ");

const PATHS = {
  setPasswordPage: "apps/web/app/(all)/accounts/set-password/page.tsx",
  onboardingParent: "apps/web/core/components/onboarding/steps/profile/root.tsx",
  onboardingChild: "apps/web/core/components/onboarding/steps/profile/set-password.tsx",
  userStore: "apps/web/core/store/user/index.ts",
  signUpDoor: "apps/web/core/components/account/auth-forms/password.tsx",
  setDoor: "apps/web/core/components/account/auth-forms/set-password.tsx",
  resetDoor: "apps/web/core/components/account/auth-forms/reset-password.tsx",
  changeDoor: "apps/web/core/components/settings/profile/content/pages/security.tsx",
};

describe("set-password route renders its own form (RC 3031)", () => {
  it("imports and renders SetPasswordForm, never ResetPasswordForm", () => {
    const source = flat(PATHS.setPasswordPage);
    expect(source).toMatch(/<SetPasswordForm \/>/);
    expect(source).not.toMatch(/<ResetPasswordForm \/>/);
  });
});

describe("the override flag reaches the server on every door (RC 3038 mutation bar)", () => {
  // M1: drop the onboarding payload spread.
  it("onboarding setPassword call carries accept_weak_password", () => {
    expect(flat(PATHS.onboardingParent)).toContain(
      "authService.setPassword(token, { password, ...(acceptWeakPassword && { accept_weak_password: true }) })"
    );
  });

  // M2: restore the store's {password}-only forwarding.
  it("user store forwards the WHOLE payload to setPassword", () => {
    const source = flat(PATHS.userStore);
    expect(source).toContain("this.authService.setPassword(csrfToken, data)");
    expect(source).not.toContain("this.authService.setPassword(csrfToken, { password: data.password })");
  });

  // The set/reset/change doors must each SEND the flag, not merely hold state.
  it("set-password door sends the flag", () => {
    expect(flat(PATHS.setDoor)).toContain("...(acceptWeakPassword && { accept_weak_password: true })");
  });

  it("reset-password door posts the flag as a form field", () => {
    expect(flat(PATHS.resetDoor)).toMatch(
      /\{acceptWeakPassword && <input type="hidden" name="accept_weak_password" value="True" \/>\}/
    );
  });

  it("sign-up door posts the flag as a form field", () => {
    expect(flat(PATHS.signUpDoor)).toMatch(/name="accept_weak_password" value="True"/);
  });

  it("profile-change door sends the flag", () => {
    expect(flat(PATHS.changeDoor)).toContain("...(acceptWeakPassword && { accept_weak_password: true })");
  });
});

describe("the override has a reachable control on every door (RC 3035)", () => {
  // M4: child checkbox callback becomes a no-op.
  it("onboarding child's checkbox invokes the callback with the toggled value", () => {
    const source = flat(PATHS.onboardingChild);
    expect(source).toContain("onChange={() => onAcceptWeakPasswordChange(!acceptWeakPassword)}");
    expect(source).toContain("checked={acceptWeakPassword}");
  });

  it("onboarding parent supplies the producer", () => {
    expect(flat(PATHS.onboardingParent)).toContain("onAcceptWeakPasswordChange={setAcceptWeakPassword}");
  });

  it.each([
    ["sign-up", PATHS.signUpDoor],
    ["set-password", PATHS.setDoor],
    ["reset-password", PATHS.resetDoor],
    ["profile change", PATHS.changeDoor],
  ])("%s door's checkbox actually toggles the state", (_door, path) => {
    // A setter named but never invoked is dead state (the RC 3035 defect).
    expect(flat(path)).toMatch(/setAcceptWeakPassword\((?:\(prev\) => !prev|!acceptWeakPassword|true|false)\)/);
  });
});

describe("onboarding cannot advance past a failed submit (RC 3036)", () => {
  // M3: catch returns true.
  it("reports failure from the catch and returns early on it", () => {
    const source = flat(PATHS.onboardingParent);
    expect(source).toContain("return false; } };"); // the catch's terminal statement
    expect(source).toContain("const succeeded = await handleSubmitUserDetail(formData);");
    expect(source).toContain("if (!succeeded) return;");
    // The success path must still report true, or the guard blocks everything.
    expect(source).toContain("return true; } catch");
  });
});

describe("the override is VISIBLE when it must be (Morrow re-gate on c247468: visibility reversion)", () => {
  // Morrow's false-green mutant: the render gate reverts to server-verdict-only,
  // restoring the exact defect the browser walk found — the checkbox invisible for
  // a typed-weak password until the server has already rejected once.
  it("onboarding child renders the checkbox for server verdict OR child-local typed weakness", () => {
    expect(flat(PATHS.onboardingChild)).toContain(
      "{(showWeakPasswordOverride || isTypedPasswordWeak) && onAcceptWeakPasswordChange && ("
    );
  });

  // Typed weakness must derive from state the child actually OWNS — the parent's
  // watch("password") was the original permanently-false gate (field never registered).
  it("typed weakness derives from the child's own passwordState", () => {
    expect(flat(PATHS.onboardingChild)).toContain(
      "passwordState.password.length > 0 && getPasswordStrength(passwordState.password) !== E_PASSWORD_STRENGTH.STRENGTH_VALID"
    );
  });

  // John (prod walk, 7/30): a control the user must act on can never sit inside a
  // collapsed section. The fields render expanded by default (consistent with the
  // sign-up door)…
  it("password section renders expanded by default", () => {
    expect(flat(PATHS.onboardingChild)).toContain("const [isExpanded, setIsExpanded] = useState(true)");
  });

  // …and a server rejection force-opens the section even if the user collapsed it.
  it("server rejection force-opens the section", () => {
    expect(flat(PATHS.onboardingChild)).toContain(
      "useEffect(() => { if (showWeakPasswordOverride) setIsExpanded(true); }, [showWeakPasswordOverride])"
    );
  });
});
