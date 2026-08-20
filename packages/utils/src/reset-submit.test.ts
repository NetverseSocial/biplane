import { describe, expect, it, vi } from "vitest";
import {
  createSubmitLock,
  runResetSubmit,
  type TResetHandlerDeps,
  classifyResetResponse,
  acceptsSubmit,
  showsWeakOverride,
  submitReset,
  WEAK_PASSWORD_ERROR_MESSAGE,
  type TResetSubmitDeps,
} from "./signup-outcome";

// BIP-29: the reset counterpart of BIP-22. A weak-password bounce must keep
// the user on the intact form; every other outcome follows the redirect.

const ORIGIN = "http://localhost:3000";
const WEAK_URL = `${ORIGIN}/accounts/reset-password/?error_message=${WEAK_PASSWORD_ERROR_MESSAGE}`;
const POST_ENDPOINT = `${ORIGIN}/auth/reset-password/uid/token/`;
const OK_URL = `${ORIGIN}/sign-in/`;

const makeDeps = (o: Partial<TResetSubmitDeps> = {}) =>
  ({
    buildBody: vi.fn(() => "password=hunter2" as unknown as BodyInit),
    postForm: vi.fn(async () => ({ url: OK_URL, status: 200, redirected: true })),
    navigate: vi.fn(),
    onWeakPassword: vi.fn(),
    onIndeterminate: vi.fn(),
    onServerError: vi.fn(),
    lock: createSubmitLock(),
    ...o,
  }) satisfies TResetSubmitDeps;

describe("submitReset", () => {
  it("stays on the form for a weak-password bounce — never navigates", async () => {
    const deps = makeDeps({ postForm: vi.fn(async () => ({ url: WEAK_URL, status: 200, redirected: true })) });
    await expect(submitReset(deps)).resolves.toBe("weak-password");
    expect(deps.onWeakPassword).toHaveBeenCalledOnce();
    expect(deps.navigate).not.toHaveBeenCalled(); // the whole bug: navigation wiped the form
  });

  it("follows the server redirect on success", async () => {
    const deps = makeDeps();
    await expect(submitReset(deps)).resolves.toBe("navigated");
    expect(deps.navigate).toHaveBeenCalledWith(OK_URL);
    expect(deps.onWeakPassword).not.toHaveBeenCalled();
  });

  it("does not auto-retry a transport-unknown outcome (reset tokens are one-time)", async () => {
    const postForm = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    const deps = makeDeps({ postForm });
    await expect(submitReset(deps)).resolves.toBe("indeterminate");
    expect(deps.onIndeterminate).toHaveBeenCalledOnce();
    expect(postForm).toHaveBeenCalledOnce();
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it("rebuilds the body per attempt (the accept_weak_password input appears only after the tick)", async () => {
    const buildBody = vi.fn(() => "b" as unknown as BodyInit);
    await submitReset(makeDeps({ buildBody }));
    expect(buildBody).toHaveBeenCalledOnce();
  });
});

describe("reset form decisions (BIP-29 / RC 3205)", () => {
  const base = {
    bouncedWeak: false,
    serverSaidWeak: false,
    clientMeterValid: true,
    passwordLength: 12,
    inFlight: false,
  };

  it("offers the override when the SERVER said weak even though the client meter is happy", () => {
    // The RC 3205 blocker: banner shown, checkbox absent, user stuck.
    expect(showsWeakOverride({ ...base, serverSaidWeak: true })).toBe(true);
  });

  it("offers the override after a URL bounce", () => {
    expect(showsWeakOverride({ ...base, bouncedWeak: true })).toBe(true);
  });

  it("offers the override when the client meter dislikes a typed password", () => {
    expect(showsWeakOverride({ ...base, clientMeterValid: false })).toBe(true);
  });

  it("does not offer it unprompted", () => {
    expect(showsWeakOverride(base)).toBe(false);
    expect(showsWeakOverride({ ...base, passwordLength: 0, clientMeterValid: false })).toBe(false);
  });

  it("refuses a second submit while one is in flight (one-time token)", () => {
    expect(acceptsSubmit(base)).toBe(true);
    expect(acceptsSubmit({ ...base, inFlight: true })).toBe(false);
  });
});

describe("single-flight lock (RC 3206 — the guard must be killable)", () => {
  it("refuses a concurrent second submit: exactly ONE post leaves", async () => {
    // The one-time token must never be sent twice. Deleting tryStart's
    // assignment in createSubmitLock turns this red.
    let resolvePost: (v: { url: string; status: number; redirected: boolean }) => void = () => {};
    const postForm = vi.fn(
      () => new Promise<{ url: string; status: number; redirected: boolean }>((r) => (resolvePost = r))
    );
    const deps = makeDeps({ postForm });
    const first = submitReset(deps);
    const second = await submitReset(deps); // while the first is still in flight
    expect(second).toBe("rejected-duplicate");
    expect(postForm).toHaveBeenCalledTimes(1);
    resolvePost({ url: OK_URL, status: 200, redirected: true });
    await first;
  });

  it("re-arms after a weak-password bounce so the user can submit again", async () => {
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: WEAK_URL, status: 200, redirected: true })),
    });
    await submitReset(deps);
    expect(deps.lock.isInFlight).toBe(false);
    await expect(submitReset(deps)).resolves.toBe("weak-password"); // not rejected
  });

  it("re-arms after an unknown outcome", async () => {
    const deps = makeDeps({
      postForm: vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    });
    await submitReset(deps);
    expect(deps.lock.isInFlight).toBe(false);
  });

  it("stays locked after navigating — the page is leaving", async () => {
    const deps = makeDeps();
    await submitReset(deps);
    expect(deps.lock.isInFlight).toBe(true);
  });
});

describe("non-redirect error responses (RC 3206 — the wipe-via-GET bug)", () => {
  it.each([403])("does NOT navigate to the POST endpoint on a resolved %i", async (status) => {
    // Navigating here GETs the POST endpoint: an error page, form wiped —
    // exactly the defect BIP-29 exists to remove.
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: POST_ENDPOINT, status, redirected: false })),
    });
    await expect(submitReset(deps)).resolves.toBe("server-error");
    expect(deps.navigate).not.toHaveBeenCalled();
    expect(deps.onServerError).toHaveBeenCalledWith(status);
    expect(deps.onIndeterminate).not.toHaveBeenCalled(); // it reached the server; not unknown
  });

  it("still navigates on a redirected 200", async () => {
    const deps = makeDeps();
    await expect(submitReset(deps)).resolves.toBe("navigated");
    expect(deps.navigate).toHaveBeenCalledWith(OK_URL);
  });

  it("classifies directly, too (fail-closed contract, RC 3209)", () => {
    expect(classifyResetResponse(POST_ENDPOINT, 403, false)).toBe("server-error");
    // 5xx is UNKNOWN, not a refusal: the endpoint saves the password before it
    // builds the redirect, so a 500 can land after the change succeeded.
    expect(classifyResetResponse(POST_ENDPOINT, 500, false)).toBe("indeterminate");
    expect(classifyResetResponse(WEAK_URL, 200, true)).toBe("weak-password");
    expect(classifyResetResponse(OK_URL, 200, true)).toBe("navigate");
    // Navigation requires a redirect AND a sub-400 status. An error status is
    // never followed, redirect or not — following it wipes the form.
    expect(classifyResetResponse(OK_URL, 403, true)).toBe("server-error");
    expect(classifyResetResponse(OK_URL, 500, true)).toBe("indeterminate");
    // A non-redirect 200 is not this endpoint's shape: fail closed.
    expect(classifyResetResponse(POST_ENDPOINT, 200, false)).toBe("indeterminate");
    // The weak marker is only trusted on a redirect.
    expect(classifyResetResponse(WEAK_URL, 200, false)).toBe("indeterminate");
  });
});

describe("no native retry of a one-time token (RC 3206)", () => {
  it("an unknown outcome posts exactly once and never navigates", async () => {
    const postForm = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    const deps = makeDeps({ postForm });
    await expect(submitReset(deps)).resolves.toBe("indeterminate");
    expect(postForm).toHaveBeenCalledTimes(1);
    expect(deps.navigate).not.toHaveBeenCalled();
    expect(deps.onIndeterminate).toHaveBeenCalledOnce();
  });
});

describe("the whole callback is testable (RC 3209 — native submit must be detectable)", () => {
  const makeForm = () => ({
    action: `${ORIGIN}/auth/reset-password/uid/token/`,
    buildBody: vi.fn(() => "password=hunter2" as unknown as BodyInit),
    // Present ONLY to prove it is never called. If anyone restores a native
    // form.submit() anywhere in this path, every test below turns red.
    submit: vi.fn(),
  });
  const makeHandlerDeps = (o: Partial<TResetHandlerDeps> = {}) =>
    ({
      lock: createSubmitLock(),
      post: vi.fn(async () => ({ url: OK_URL, status: 200, redirected: true })),
      navigate: vi.fn(),
      onPending: vi.fn(),
      onWeakPassword: vi.fn(),
      onServerError: vi.fn(),
      onIndeterminate: vi.fn(),
      ...o,
    }) satisfies TResetHandlerDeps;

  it("never calls a native submit on an unknown outcome", async () => {
    const form = makeForm();
    const deps = makeHandlerDeps({
      post: vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    });
    await runResetSubmit(form, deps);
    expect(form.submit).not.toHaveBeenCalled(); // the RC 3205 regression, now detectable
    expect(deps.onIndeterminate).toHaveBeenCalledOnce();
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it.each([
    ["weak bounce", { url: WEAK_URL, status: 200, redirected: true }],
    ["non-redirect 403", { url: POST_ENDPOINT, status: 403, redirected: false }],
    ["500", { url: POST_ENDPOINT, status: 500, redirected: false }],
    ["non-redirect 200", { url: POST_ENDPOINT, status: 200, redirected: false }],
  ])("never calls a native submit on %s", async (_label, response) => {
    const form = makeForm();
    await runResetSubmit(form, makeHandlerDeps({ post: vi.fn(async () => response) }));
    expect(form.submit).not.toHaveBeenCalled();
  });

  it("clears pending on every non-navigating outcome", async () => {
    const form = makeForm();
    const deps = makeHandlerDeps({
      post: vi.fn(async () => ({ url: WEAK_URL, status: 200, redirected: true })),
    });
    await runResetSubmit(form, deps);
    expect(deps.onPending).toHaveBeenNthCalledWith(1, true);
    expect(deps.onPending).toHaveBeenLastCalledWith(false);
  });

  it("rejects a duplicate submit and does NOT clear pending while the first still owns the lock", async () => {
    const form = makeForm();
    let resolvePost: (v: { url: string; status: number; redirected: boolean }) => void = () => {};
    const deps = makeHandlerDeps({
      post: vi.fn(() => new Promise<{ url: string; status: number; redirected: boolean }>((r) => (resolvePost = r))),
    });
    const first = runResetSubmit(form, deps);
    await expect(runResetSubmit(form, deps)).resolves.toBe("rejected-duplicate");
    expect(deps.post).toHaveBeenCalledTimes(1);
    // THE ASSERTION RC 3215 required: the rejected duplicate must not touch
    // pending at all. Previously the events were true, true, false — the
    // second call cleared pending while the first was still in flight, which
    // re-enables the button mid-submit of a ONE-TIME token.
    expect(deps.onPending.mock.calls.map((c) => c[0])).toEqual([true]);
    expect(deps.lock.isInFlight).toBe(true);
    resolvePost({ url: OK_URL, status: 200, redirected: true });
    await first;
    // and after the first completes by navigating, the lock stays held
    expect(deps.lock.isInFlight).toBe(true);
  });
});

describe("fail-closed status contract (RC 3209)", () => {
  it("names the status in the unknown diagnostic (Morrow: the interpolation was silently dropped)", async () => {
    // A shell edit once turned this message into "(status )" — a diagnostic
    // that cannot diagnose. Pin the ARGUMENT, not just the callback.
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: POST_ENDPOINT, status: 503, redirected: false })),
    });
    await submitReset(deps);
    const err = deps.onIndeterminate.mock.calls[0][0] as Error;
    expect(String(err.message)).toContain("503");
    expect(String(err.message)).not.toMatch(/status \)/);
  });

  it("treats 5xx as UNKNOWN, not a refusal — the save precedes the redirect", async () => {
    // apps/api .../password_management.py calls user.save() BEFORE building the
    // redirect, with no transaction: a 5xx can arrive AFTER the password
    // changed. Claiming "not changed" would be a lie the user acts on.
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: POST_ENDPOINT, status: 500, redirected: false })),
    });
    await expect(submitReset(deps)).resolves.toBe("indeterminate");
    expect(deps.onIndeterminate).toHaveBeenCalledOnce();
    expect(deps.onServerError).not.toHaveBeenCalled();
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it.each([500, 502, 503])("a redirected %i is still unknown, never navigated", async (status) => {
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: OK_URL, status, redirected: true })),
    });
    await expect(submitReset(deps)).resolves.toBe("indeterminate");
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it("a redirected 403 is a refusal, never navigated", async () => {
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: OK_URL, status: 403, redirected: true })),
    });
    await expect(submitReset(deps)).resolves.toBe("server-error");
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it("a NON-redirect 200 is unknown, never navigated (it would GET the POST endpoint)", async () => {
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: POST_ENDPOINT, status: 200, redirected: false })),
    });
    await expect(submitReset(deps)).resolves.toBe("indeterminate");
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it("only a redirected sub-400 response navigates", async () => {
    const deps = makeDeps();
    await expect(submitReset(deps)).resolves.toBe("navigated");
    expect(deps.navigate).toHaveBeenCalledWith(OK_URL);
  });

  it("a weak marker on a NON-redirect response is not trusted", async () => {
    // Reading the marker off the POST endpoint's own body would let the
    // endpoint's content decide the flow.
    const deps = makeDeps({
      postForm: vi.fn(async () => ({ url: WEAK_URL, status: 200, redirected: false })),
    });
    await expect(submitReset(deps)).resolves.toBe("indeterminate");
    expect(deps.onWeakPassword).not.toHaveBeenCalled();
  });
});
