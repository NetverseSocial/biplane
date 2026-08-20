import { describe, expect, it } from "vitest";
import { classifySignUpOutcome, WEAK_PASSWORD_ERROR_MESSAGE } from "./signup-outcome";

/**
 * BIP-22, Morrow RC 3109.
 *
 * The case that matters is the FALSE POSITIVE: a successful sign-up whose
 * redirect carries the weak-password text because `next_path` injected it.
 * Getting that wrong strands a user on a form while their account already
 * exists, and their retry earns USER_ALREADY_EXIST — so it is worse than the
 * bug this feature fixes.
 */

const ORIGIN = "http://localhost:3000";
const WEAK = `${ORIGIN}/?error_code=5021&error_message=${WEAK_PASSWORD_ERROR_MESSAGE}&email=a%40b.c`;

// Exactly what `get_safe_redirect_url` produces for
// next_path="/dashboard&error_message=PASSWORD_TOO_WEAK": the path is
// interpolated UNENCODED, so the injected pair becomes a real parameter.
const INJECTED_SUCCESS = `${ORIGIN}/?next_path=/dashboard&error_message=${WEAK_PASSWORD_ERROR_MESSAGE}`;

describe("classifySignUpOutcome — the injection case", () => {
  it("does NOT intercept an authenticated sign-up carrying injected weak-password text", () => {
    // The regression. Before the fix this returned "weak-password" and held the
    // user on the form with their account already created.
    expect(classifySignUpOutcome(INJECTED_SUCCESS, true)).toBe("navigate");
  });

  it("authentication dominates even the genuine bounce URL", () => {
    // If a session exists the sign-up worked, whatever the query says. Stated
    // as its own case so the precedence is pinned, not incidental.
    expect(classifySignUpOutcome(WEAK, true)).toBe("navigate");
  });

  it("still intercepts a genuine bounce when NOT authenticated", () => {
    // Without this, returning "navigate" unconditionally would pass every test
    // above and silently delete the feature.
    expect(classifySignUpOutcome(WEAK, false)).toBe("weak-password");
  });

  it("intercepts the injected-looking URL when NOT authenticated", () => {
    // Same URL as the first case. Only the session differs — which is the
    // point: the URL alone cannot decide this.
    expect(classifySignUpOutcome(INJECTED_SUCCESS, false)).toBe("weak-password");
  });
});

describe("classifySignUpOutcome — everything else navigates", () => {
  it.each([
    [`${ORIGIN}/onboarding/`, "a plain success redirect"],
    [`${ORIGIN}/?error_code=5047&error_message=INVALID_NAME_SIGN_UP`, "a DIFFERENT error"],
    [`${ORIGIN}/?error_message=password_too_weak`, "lowercase — not the exact code"],
    [`${ORIGIN}/?error_message=PASSWORD_TOO_WEAK_SUFFIX`, "a superstring of the code"],
    [`${ORIGIN}/?error_msg=PASSWORD_TOO_WEAK`, "the value under the wrong key"],
    [`${ORIGIN}/`, "no query at all"],
  ])("navigates for %s (%s)", (url) => {
    expect(classifySignUpOutcome(url, false)).toBe("navigate");
  });

  it.each([["not a url"], [""], ["///"]])("navigates rather than trapping on unparseable %j", (url) => {
    // Failing closed here would mean stranding the user on a form because we
    // could not parse a string. The server's response still decides where they
    // go.
    expect(classifySignUpOutcome(url, false)).toBe("navigate");
  });
});
