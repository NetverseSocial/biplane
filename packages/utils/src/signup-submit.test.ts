import { describe, expect, it, vi } from "vitest";
import { submitSignUp, WEAK_PASSWORD_ERROR_MESSAGE, type TSignUpSubmitDeps } from "./signup-outcome";

/**
 * BIP-22, the submission seam.
 *
 * RC 3109 required four behaviours. RC 3123 then found two transport-unknown
 * branches the first version got wrong, and SUPERSEDED one of the four: there
 * is no longer an automatic native-submit fallback, because a rejected POST
 * does not prove the POST never landed and sign-up is not idempotent.
 *
 * Each block below is written so that removing the behaviour makes it red.
 */

const ORIGIN = "http://localhost:3000";
const WEAK_URL = `${ORIGIN}/?error_code=5021&error_message=${WEAK_PASSWORD_ERROR_MESSAGE}&email=a%40b.c`;
const SUCCESS_URL = `${ORIGIN}/onboarding/`;
// A SUCCESSFUL sign-up whose redirect carries the weak token because next_path
// was interpolated into the query unencoded.
const INJECTED_SUCCESS_URL = `${ORIGIN}/?next_path=/dashboard&error_message=${WEAK_PASSWORD_ERROR_MESSAGE}`;

const makeDeps = (overrides: Partial<TSignUpSubmitDeps> = {}) =>
  ({
    buildBody: vi.fn(() => "password=hunter2" as unknown as BodyInit),
    postForm: vi.fn(async () => ({ url: SUCCESS_URL })),
    checkAuthenticated: vi.fn(async () => true),
    navigate: vi.fn(),
    onWeakPassword: vi.fn(),
    onIndeterminate: vi.fn(),
    ...overrides,
  }) satisfies TSignUpSubmitDeps;

describe("1. a genuine weak-password rejection does not navigate", () => {
  it("shows the banner and stays on the page", async () => {
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: WEAK_URL })),
      checkAuthenticated: vi.fn(async () => false),
    });

    await expect(submitSignUp(deps)).resolves.toBe("weak-password");

    expect(deps.onWeakPassword).toHaveBeenCalledOnce();
    // The navigation was the whole bug: it wipes the form the banner has just
    // asked the user to act on.
    expect(deps.navigate).not.toHaveBeenCalled();
  });
});

describe("2. a successful redirect navigates even when its query says PASSWORD_TOO_WEAK", () => {
  it("does not intercept the next_path injection case", async () => {
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: INJECTED_SUCCESS_URL })),
      checkAuthenticated: vi.fn(async () => true),
    });

    await expect(submitSignUp(deps)).resolves.toBe("navigated");

    expect(deps.navigate).toHaveBeenCalledWith(INJECTED_SUCCESS_URL);
    // Worse than the original bug: the account exists, so the retry the banner
    // invites earns USER_ALREADY_EXIST.
    expect(deps.onWeakPassword).not.toHaveBeenCalled();
  });

  it("asks the server rather than reading the answer off the URL", async () => {
    const deps = makeDeps({ postForm: vi.fn(async () => ({ url: INJECTED_SUCCESS_URL })) });
    await submitSignUp(deps);
    expect(deps.checkAuthenticated).toHaveBeenCalledOnce();
  });
});

describe("3. the checked retry carries the override and proceeds", () => {
  it("rebuilds the body on the second attempt and navigates", async () => {
    // The hidden accept_weak_password input does not exist in the form until
    // the box is ticked, so a cached body would resubmit the rejected password
    // forever.
    const bodies: string[] = [];
    let checked = false;
    const deps = makeDeps({
      buildBody: vi.fn(() => {
        const body = checked ? "password=hunter2&accept_weak_password=True" : "password=hunter2";
        bodies.push(body);
        return body as unknown as BodyInit;
      }),
      postForm: vi.fn(async (body) => ({
        url: String(body).includes("accept_weak_password") ? SUCCESS_URL : WEAK_URL,
      })),
      checkAuthenticated: vi.fn(async () => String(bodies.at(-1)).includes("accept_weak_password")),
    });

    await expect(submitSignUp(deps)).resolves.toBe("weak-password");
    checked = true; // the user ticks "use it anyway"
    await expect(submitSignUp(deps)).resolves.toBe("navigated");

    expect(bodies).toEqual(["password=hunter2", "password=hunter2&accept_weak_password=True"]);
    expect(deps.navigate).toHaveBeenCalledWith(SUCCESS_URL);
  });

  it("builds the body exactly once per attempt, and posts that body", async () => {
    const body = "password=hunter2" as unknown as BodyInit;
    const buildBody = vi.fn(() => body);
    const postForm = vi.fn(async () => ({ url: SUCCESS_URL }));

    await submitSignUp(makeDeps({ buildBody, postForm }));

    expect(buildBody).toHaveBeenCalledOnce();
    expect(postForm).toHaveBeenCalledWith(body);
  });
});

describe("4. RC 3123 — a rejected POST is AMBIGUOUS, never a licence to resubmit", () => {
  it("does not resubmit, and reports the outcome as unknown", async () => {
    // A fetch also rejects when the connection drops while the RESPONSE is in
    // flight — the account already exists by then. Sign-up is not idempotent,
    // so there is no safe automatic retry.
    const failure = new TypeError("Failed to fetch");
    const postForm = vi.fn(async () => {
      throw failure;
    });
    const deps = makeDeps({ postForm });

    await expect(submitSignUp(deps)).resolves.toBe("indeterminate");

    expect(deps.onIndeterminate).toHaveBeenCalledWith(failure);
    expect(postForm).toHaveBeenCalledOnce(); // exactly one POST ever leaves
    expect(deps.navigate).not.toHaveBeenCalled();
    expect(deps.onWeakPassword).not.toHaveBeenCalled();
  });

  it("sends at most one POST per call", async () => {
    // The failure mode is a SECOND account, so this is the assertion that
    // matters most in this file.
    const postForm = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    await submitSignUp(makeDeps({ postForm }));
    expect(postForm).toHaveBeenCalledTimes(1);
  });
});

describe("5. RC 3123 — a failed session check is UNKNOWN, not unauthenticated", () => {
  it("does not read the query string when authentication is unknown", async () => {
    // The first version coerced this to `false` and then classified from the
    // query, walking straight back into the injection case: a SUCCESSFUL
    // sign-up whose URL carries the weak token.
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: INJECTED_SUCCESS_URL })),
      checkAuthenticated: vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    });

    await expect(submitSignUp(deps)).resolves.toBe("navigated");

    expect(deps.onWeakPassword).not.toHaveBeenCalled();
    expect(deps.navigate).toHaveBeenCalledWith(INJECTED_SUCCESS_URL);
  });

  it("follows the server's own redirect under unknown auth, even on the genuine bounce URL", async () => {
    // Degrading to a full page load is acceptable. Falsely claiming a
    // weak-password rejection on an account that may exist is not.
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: WEAK_URL })),
      checkAuthenticated: vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    });

    await expect(submitSignUp(deps)).resolves.toBe("navigated");
    expect(deps.onWeakPassword).not.toHaveBeenCalled();
  });

  it("still intercepts when the server ANSWERS that we are not signed in", async () => {
    // Without this, returning "navigated" unconditionally would pass every
    // test above and silently delete the feature.
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: WEAK_URL })),
      checkAuthenticated: vi.fn(async () => false),
    });

    await expect(submitSignUp(deps)).resolves.toBe("weak-password");
  });

  it("does not resubmit when the session check fails", async () => {
    const postForm = vi.fn(async () => ({ url: SUCCESS_URL }));
    const deps = makeDeps({
      postForm,
      checkAuthenticated: vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    });

    await submitSignUp(deps);
    expect(postForm).toHaveBeenCalledOnce();
  });
});
