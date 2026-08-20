/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

export const WEAK_PASSWORD_ERROR_MESSAGE = "PASSWORD_TOO_WEAK";

export type TSignUpOutcome = "weak-password" | "navigate";

/**
 * @description Decide what a sign-up POST's outcome was.
 *
 * biplane (BIP-22, Morrow RC 3109): this exists as a pure exported function for
 * two reasons, and both are the review's.
 *
 * WHY IT IS NOT A QUERY-STRING CHECK
 *
 * The first version read `error_message=PASSWORD_TOO_WEAK` off the final URL
 * and treated that as proof of a weak-password bounce. That query text is
 * attacker-influenceable. `next_path` is interpolated into the redirect's query
 * string UNENCODED by `get_safe_redirect_url`, and `validate_next_path` does
 * not reject `&` or `=` — so a `next_path` of
 *
 *     /dashboard&error_message=PASSWORD_TOO_WEAK
 *
 * survives validation and puts that parameter on a SUCCESSFUL sign-up's
 * redirect. The old check would then have held the user on the form, showing
 * "use a stronger password", while their account already existed — and their
 * natural retry earns USER_ALREADY_EXIST.
 *
 * So the decision is made on something the URL cannot forge: whether the
 * request actually authenticated. `isAuthenticated` DOMINATES. If a session
 * exists the sign-up succeeded, whatever the query string claims.
 *
 * WHY IT IS HERE AND NOT INLINE IN THE FORM
 *
 * It was a branch inside a 61-line submit handler in `apps/web`, which has no
 * test runner — so the coordinator had no executable net in the repository and
 * the injection case could not be pinned. `packages/utils` has one. Extracting
 * the decision makes the case above a test rather than a claim.
 *
 * @param finalUrl the URL the sign-up POST ended on, after redirects
 * @param isAuthenticated whether a session now exists (ask the server; do not infer)
 */
export const classifySignUpOutcome = (finalUrl: string, isAuthenticated: boolean): TSignUpOutcome => {
  // An authenticated caller signed up successfully. No query text can override
  // that, which is the whole point.
  if (isAuthenticated) return "navigate";

  try {
    const parsed = new URL(finalUrl);
    if (parsed.searchParams.get("error_message") === WEAK_PASSWORD_ERROR_MESSAGE) {
      return "weak-password";
    }
  } catch {
    // Not a parseable URL. Fall through to navigate rather than trapping the
    // user on a form we cannot reason about — the server's response is still
    // the authority on where they go.
  }

  return "navigate";
};

export type TSignUpSubmitResult = "weak-password" | "navigated" | "indeterminate";

export type TSignUpSubmitDeps = {
  /**
   * Build the request body. Called once PER ATTEMPT, never cached — the whole
   * point of the retry is that the form has changed since the last one (the
   * `accept_weak_password` override is only in the form after the user ticks
   * the box), so a body captured on the first submit would resubmit the
   * rejected password and bounce again forever.
   */
  buildBody: () => BodyInit;
  /** POST the sign-up and follow redirects. A rejection is AMBIGUOUS — see below. */
  postForm: (body: BodyInit) => Promise<{ url: string }>;
  /** Ask the SERVER whether a session now exists. A rejection means UNKNOWN. */
  checkAuthenticated: () => Promise<boolean>;
  /** Leave the page. */
  navigate: (url: string) => void;
  /** Surface the weak-password banner and override checkbox, over the intact form. */
  onWeakPassword: () => void;
  /**
   * The outcome could not be determined and the account MAY already exist.
   * Tell the user plainly and let them decide. This must never trigger an
   * automatic retry.
   */
  onIndeterminate: (error: unknown) => void;
};

/**
 * @description Run a sign-up submission and decide where the user ends up.
 *
 * biplane (BIP-22). RC 3109 asked for this seam to be testable at all; RC 3123
 * then found two transport-unknown branches the first version got wrong. Both
 * corrections are the same mistake in different places: **treating "I do not
 * know" as "no".**
 *
 * 1. A REJECTED POST DOES NOT PROVE THE POST NEVER LANDED.
 *
 *    The first version asserted exactly that, in a comment: "the request never
 *    landed, so a native submit cannot double-create anything." That was an
 *    assumption written as a fact. A fetch also rejects when the connection
 *    drops while the RESPONSE is in flight — by which time the account exists.
 *    Automatically re-POSTing then either creates a second account or earns
 *    USER_ALREADY_EXIST on a sign-up that actually succeeded, which looks to
 *    the user exactly like the bug this whole change fixes.
 *
 *    Sign-up is not idempotent, so there is no safe automatic retry. The user
 *    is told, and the user decides.
 *
 * 2. A FAILED SESSION CHECK IS UNKNOWN, NOT UNAUTHENTICATED.
 *
 *    The first version coerced it to `false` and then read the query string —
 *    walking straight back into the injection case RC 3109 was about, where a
 *    SUCCESSFUL sign-up carries `error_message=PASSWORD_TOO_WEAK` because
 *    `next_path` is interpolated into the redirect unencoded. Under unknown
 *    authentication the query is not evidence of anything, so it is not
 *    consulted; the server's own redirect target is followed instead.
 *
 * The query string is read in exactly one situation: we asked the server, the
 * server answered, and the answer was "not signed in".
 */
export const submitSignUp = async (deps: TSignUpSubmitDeps): Promise<TSignUpSubmitResult> => {
  let response: { url: string };
  try {
    response = await deps.postForm(deps.buildBody());
  } catch (error) {
    // Ambiguous by construction. Do not resubmit, do not guess.
    deps.onIndeterminate(error);
    return "indeterminate";
  }

  let isAuthenticated: boolean | "unknown";
  try {
    isAuthenticated = await deps.checkAuthenticated();
  } catch {
    isAuthenticated = "unknown";
  }

  // Signed in: the sign-up worked, whatever the query claims.
  if (isAuthenticated === true) {
    deps.navigate(response.url);
    return "navigated";
  }

  // Unknown: follow the server's own redirect. Reading the query here is
  // precisely what reintroduced the false positive, so it is not done.
  if (isAuthenticated === "unknown") {
    deps.navigate(response.url);
    return "navigated";
  }

  if (classifySignUpOutcome(response.url, false) === "weak-password") {
    deps.onWeakPassword();
    return "weak-password";
  }

  deps.navigate(response.url);
  return "navigated";
};

/** The fields the contract needs. status and redirected are REQUIRED, not
 * optional (Morrow): the production adapter always supplies them, and a
 * defaulted status silently re-enables the navigate-anything behaviour the
 * fail-closed contract exists to prevent. */
export type TResetResponse = {
  url: string;
  status: number;
  redirected: boolean;
};

export type TResetSubmitDeps = {
  /** Build the request body. Called once per attempt, never cached — the
   * accept_weak_password input only exists after the user ticks the box. */
  buildBody: () => BodyInit;
  /** POST the reset and follow redirects. A rejection is AMBIGUOUS. */
  postForm: (body: BodyInit) => Promise<TResetResponse>;
  /** Leave the page (the server's own redirect target). */
  navigate: (url: string) => void;
  /** Weak-password bounce: banner + override checkbox over an INTACT form. */
  onWeakPassword: () => void;
  /** Outcome unknown (transport failure). Reset tokens are one-time, so no
   * automatic retry: the user is told and decides. */
  onIndeterminate: (error: unknown) => void;
  /** The server REFUSED with a 4xx: the request reached the server and was
   * rejected before any write, so "not changed" is safe to say. Never
   * navigable — the URL may be the POST endpoint, and GETting it wipes the
   * form. A 5xx is NOT this case: it goes to onIndeterminate, because the
   * endpoint saves the password before it builds the redirect. */
  onServerError: (status: number) => void;
  /** The single-flight lock. Owned by the caller so the component holds one
   * instance across renders; the coordinator does the locking (RC 3206). */
  lock: TSubmitLock;
};

/**
 * @description Run a password-reset submission without ever leaving the page
 * on a weak-password bounce (BIP-29).
 *
 * The defect this closes is BIP-22's, on the other form that
 * navigates on the server's redirect. A weak-password bounce carries
 * PASSWORD_TOO_WEAK on the final URL; that alone keeps the user on the intact
 * form. Reset has no session to check (it does not sign the user in), so it is
 * simpler than submitSignUp: post, then branch on the URL.
 */
export const submitReset = async (
  deps: TResetSubmitDeps
): Promise<TSignUpSubmitResult | "server-error" | "rejected-duplicate"> => {
  // The lock is taken HERE, inside the tested unit (RC 3206): a component-level
  // ref could be deleted without any test noticing.
  if (!deps.lock.tryStart()) return "rejected-duplicate";

  let response: TResetResponse;
  try {
    response = await deps.postForm(deps.buildBody());
  } catch (error) {
    // A reset token is one-time; a transport-unknown outcome must not
    // auto-retry — not here, and not by falling back to a native submit.
    deps.lock.release();
    deps.onIndeterminate(error);
    return "indeterminate";
  }

  const outcome = classifyResetResponse(response.url, response.status, response.redirected);

  if (outcome === "indeterminate") {
    // 5xx, or any shape that is not a definite outcome. The password MAY have
    // been changed — the endpoint saves before it builds the redirect, in no
    // transaction — so nothing is retried and nothing is claimed (RC 3209).
    deps.lock.release();
    deps.onIndeterminate(new Error(`reset outcome unknown (status ${response.status})`));
    return "indeterminate";
  }

  if (outcome === "server-error") {
    deps.lock.release();
    deps.onServerError(response.status);
    return "server-error";
  }

  if (outcome === "weak-password") {
    deps.lock.release();
    deps.onWeakPassword();
    return "weak-password";
  }

  // Navigating: the page is leaving, so the lock is deliberately NOT released.
  deps.navigate(response.url);
  return "navigated";
};

/**
 * @description The reset form's three UI decisions, as data (BIP-29, Morrow
 * RC 3205).
 *
 * apps/web has no test runner, so a behaviour that lives only in JSX can be
 * pinned by nothing but a source grep — which is exactly what RC 3205 refused
 * to accept. These predicates are the same decisions the form makes, in a
 * package that HAS tests, so each one is executable.
 */
export type TResetFormState = {
  /** the URL carried a PASSWORD_TOO_WEAK bounce */
  bouncedWeak: boolean;
  /** this session's fetch came back weak */
  serverSaidWeak: boolean;
  /** what the client-side meter thinks (it is WEAKER than the server's zxcvbn) */
  clientMeterValid: boolean;
  passwordLength: number;
  /** a submit is in flight right now */
  inFlight: boolean;
};

/** The override checkbox must be reachable whenever the SERVER said weak —
 * a client-valid password can still be server-rejected, and without this the
 * user has a banner telling them to tick a box that is not rendered. */
export const showsWeakOverride = (s: TResetFormState): boolean =>
  s.bouncedWeak || s.serverSaidWeak || (s.passwordLength > 0 && !s.clientMeterValid);

/** A one-time token must never be posted twice: while a submit is in flight,
 * further submits are refused SYNCHRONOUSLY (a re-render is too late). */
export const acceptsSubmit = (s: TResetFormState): boolean => !s.inFlight;

/**
 * @description Stateful single-flight coordinator for a ONE-TIME token submit
 * (BIP-29, Morrow RC 3206).
 *
 * RC 3205 added a ref latch inline in the component. RC 3206 rejected that:
 * apps/web has no test runner, so deleting the ref assignment left every test
 * green — the guard was unpinned. The lock therefore lives HERE, as state a
 * test can mutate and kill.
 *
 * `tryStart()` returns false while a submit is in flight, so a second click
 * cannot post a one-time reset token twice. `release()` re-arms it for the
 * cases where the user legitimately submits again (weak-password bounce,
 * unknown outcome). A successful navigation never releases — the page is
 * leaving.
 */
export const createSubmitLock = () => {
  let inFlight = false;
  return {
    tryStart: (): boolean => {
      if (inFlight) return false;
      inFlight = true;
      return true;
    },
    release: (): void => {
      inFlight = false;
    },
    get isInFlight(): boolean {
      return inFlight;
    },
  };
};

export type TSubmitLock = ReturnType<typeof createSubmitLock>;

/**
 * @description Classify a resolved reset POST that did NOT redirect
 * (BIP-29, Morrow RC 3206).
 *
 * The bug this closes: submitReset treated ANY resolved response as
 * success-or-weak and navigated to `response.url`. For a non-redirect 403 or
 * 500 that URL is the POST endpoint itself, so the browser did a GET on it —
 * an error page, and the wiped form all over again, which is the exact defect
 * BIP-29 exists to remove.
 *
 * A resolved error status is NOT unknown: the request demonstrably reached the
 * server and was refused, so the one-time token may be spent. It is reported
 * as its own outcome — no navigation, no retry.
 */
export type TResetOutcome = "weak-password" | "navigate" | "server-error" | "indeterminate";

export const classifyResetResponse = (finalUrl: string, status: number, redirected: boolean): TResetOutcome => {
  // 5xx FIRST, and it is UNKNOWN — not a refusal (Morrow RC 3209). The
  // endpoint calls user.save() BEFORE it builds the redirect, with no
  // transaction around either, so a 5xx can happen AFTER the password was
  // actually changed. Claiming "your password was not changed" here would be
  // a lie the user acts on.
  if (status >= 500) return "indeterminate";

  // A weak bounce is a REDIRECT carrying the marker. Reading the marker off a
  // non-redirect response would trust the POST endpoint's own body.
  if (redirected && status < 400 && classifySignUpOutcome(finalUrl, false) === "weak-password") {
    return "weak-password";
  }

  // A 4xx IS a refusal: the request reached the server and was rejected
  // before any write, so "not changed" is safe to say.
  if (status >= 400) return "server-error";

  // FAIL CLOSED: navigate only on a real redirect success. A non-redirect 200
  // is not the documented shape of this endpoint — navigating to it GETs the
  // POST URL and wipes the form, which is the whole defect.
  if (!redirected) return "indeterminate";

  return "navigate";
};

/**
 * @description The reset form's ENTIRE submit callback, as a testable unit
 * (BIP-29, Morrow RC 3209).
 *
 * RC 3206 moved the lock here; RC 3209 found the remaining hole: the callback
 * itself still lived in the component, so someone restoring a native
 * `form.submit()` inside it — the exact regression that wipes the form and
 * re-POSTs a one-time token — left the package suite green.
 *
 * So the callback lives here and receives the form as a MINIMAL interface.
 * The tests pass a fake form whose `submit` is a spy that must never fire:
 * any native-submit regression, anywhere in this path, turns them red.
 */
export type TResetFormLike = {
  action: string;
  buildBody: () => BodyInit;
  /** Present ONLY so tests can prove it is never called. Never invoke it. */
  submit: () => void;
};

export type TResetHandlerDeps = {
  lock: TSubmitLock;
  post: (action: string, body: BodyInit) => Promise<TResetResponse>;
  navigate: (url: string) => void;
  onPending: (pending: boolean) => void;
  onWeakPassword: () => void;
  onServerError: (status: number) => void;
  onIndeterminate: (error: unknown) => void;
};

export const runResetSubmit = async (form: TResetFormLike, deps: TResetHandlerDeps) => {
  // Check ownership BEFORE signalling pending (Morrow RC 3215). Signalling
  // first produced pending(true), pending(true), pending(false) for a
  // duplicate click — the rejected second call cleared the pending state while
  // the FIRST submit still held the lock, re-enabling the button mid-flight.
  // JS is single-threaded and there is no await between this check and
  // submitReset's tryStart, so no submit can interleave.
  if (deps.lock.isInFlight) return "rejected-duplicate" as const;
  deps.onPending(true);
  const result = await submitReset({
    lock: deps.lock,
    buildBody: form.buildBody,
    postForm: (body) => deps.post(form.action, body),
    navigate: deps.navigate,
    onWeakPassword: () => {
      deps.onPending(false);
      deps.onWeakPassword();
    },
    onServerError: (status) => {
      deps.onPending(false);
      deps.onServerError(status);
    },
    onIndeterminate: (error) => {
      // NO native submit here. A reset token is one-time and may already be
      // spent; re-POSTing it is the regression RC 3205 removed and RC 3209
      // demanded be made detectable.
      deps.onPending(false);
      deps.onIndeterminate(error);
    },
  });
  return result;
};
