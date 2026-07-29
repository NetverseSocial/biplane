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
//  I3. A superseded request must not mutate ANYTHING: no models, no toasts, no
//      loading changes (the newer request owns the flag).
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
    cb.setLoading(true);
    try {
      const models = await fetchModels(base, key);
      if (requestId !== generation) return; // superseded (I3)
      const now = getIdentity();
      if (base !== now.base || key !== now.key) return; // identity moved (I2)
      cb.setModels(models);
      if (models.length === 0) cb.onEmpty();
      else cb.onSuccess(models.length);
    } catch (err: unknown) {
      if (requestId !== generation) return; // superseded failures stay silent (I3)
      cb.onError((err as { error?: string })?.error || "Could not load models from this endpoint.");
    } finally {
      // Only the newest request owns the loading flag (I3).
      if (requestId === generation) cb.setLoading(false);
    }
  };

  return { invalidate, load };
}
