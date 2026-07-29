/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: async coordinator for the AI form's model list. A fetched list belongs
// to ONE endpoint identity (base URL + API key); this module owns the rules for
// when a response may commit and who owns the loading flag. Pure and framework-
// free so the race behavior is testable deterministically (RC 3019) — the React
// form only wires callbacks and calls invalidate()/load().
//
// Invariants (each pinned by tests/model-list-coordinator.test.ts):
//  I1. invalidate() is a CANCEL: it supersedes any in-flight request AND clears
//      the loading flag — an orphaned request must never leave the control stuck.
//  I2. A response commits only if it is the newest request (generation) AND the
//      endpoint identity it was fetched for still matches the current identity.
//  I3. A superseded request must not commit models or toasts. Loading OWNERSHIP
//      is the deliberate carve-out: it is GENERATION-only — the newest request
//      always releases the flag in its finally, even if identity drifted with no
//      successor request, because nothing else would release it. Do NOT "unify"
//      the finally onto the full currentness predicate: that reintroduces the
//      stuck-spinner wedge (RC 3018) via the RC 3023 fix.
//  I4. invalidate-then-load (the synchronous subscription ordering) must leave
//      the new load fully functional and committable.

export type TEndpointIdentity = { base: string; key: string };

export type TModelListCallbacks = {
  setModels: (models: string[]) => void;
  setLoading: (loading: boolean) => void;
  onSuccess: (count: number) => void;
  onEmpty: () => void;
  onError: (message: string) => void;
};

export type TModelListCoordinator = {
  invalidate: () => void;
  load: () => Promise<void>;
};

export function createModelListCoordinator(
  fetchModels: (base: string, key: string) => Promise<string[]>,
  getIdentity: () => TEndpointIdentity,
  cb: TModelListCallbacks
): TModelListCoordinator {
  let generation = 0;

  const invalidate = () => {
    generation += 1;
    cb.setModels([]);
    // I1 — invalidation is a cancel. The orphaned request's finally will see
    // itself superseded and skip the clear, so the cancel must do it.
    cb.setLoading(false);
  };

  const load = async () => {
    const requestId = ++generation;
    // Capture the identity this fetch is FOR (I2) — belt-and-braces alongside the
    // generation: every identity-changing input invalidates synchronously, and
    // this guard also covers any path where identity changes without one.
    const { base, key } = getIdentity();
    // Currentness is ONE predicate for success and failure alike (I2) — a stale
    // rejection must be exactly as silent as a stale success; an asymmetric catch
    // would toast endpoint A's error into endpoint B's UI.
    const isCurrent = () => {
      if (requestId !== generation) return false; // superseded (I3)
      const now = getIdentity();
      return base === now.base && key === now.key; // identity moved (I2)
    };
    cb.setLoading(true);
    try {
      const models = await fetchModels(base, key);
      if (!isCurrent()) return;
      cb.setModels(models);
      if (models.length === 0) cb.onEmpty();
      else cb.onSuccess(models.length);
    } catch (err: unknown) {
      if (!isCurrent()) return;
      cb.onError((err as { error?: string })?.error || "Could not load models from this endpoint.");
    } finally {
      // Loading OWNERSHIP is generation-only (I3): the newest request must always
      // release the flag, even if identity drifted — nothing else will.
      if (requestId === generation) cb.setLoading(false);
    }
  };

  return { invalidate, load };
}
