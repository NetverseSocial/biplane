/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: deterministic async regression for the model-list coordinator (RC 3019).
// The mechanism shipped two consecutive review heads with race defects (permanent
// spinner wedge; key-change killing the correct request), so every guard has an
// executable kill-condition here:
//  - remove invalidate()'s setLoading(false)            -> "cancel clears loading" reds
//  - remove the generation check                        -> "late A" scenarios red
//  - remove the identity check                          -> "identity moved" scenario reds
//  - restore passive-effect (post-load) key invalidation-> "invalidate-then-load" reds
import { describe, expect, it } from "vitest";

import { createModelListCoordinator, type TEndpointIdentity } from "../core/model-list-coordinator";

type Deferred = {
  promise: Promise<string[]>;
  resolve: (models: string[]) => void;
  reject: (err: unknown) => void;
};

const deferred = (): Deferred => {
  let resolve!: (models: string[]) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<string[]>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

// Harness capturing every observable mutation, in order.
const harness = () => {
  const fetches: { base: string; key: string; d: Deferred }[] = [];
  let identity: TEndpointIdentity = { base: "https://a.example/v1", key: "key-A" };
  const state = {
    models: null as string[] | null,
    loading: false,
    events: [] as string[],
  };
  const coordinator = createModelListCoordinator(
    (base, key) => {
      const d = deferred();
      fetches.push({ base, key, d });
      return d.promise;
    },
    () => identity,
    {
      setModels: (m) => {
        state.models = m;
        state.events.push(`models:${m.join(",") || "(empty)"}`);
      },
      setLoading: (l) => {
        state.loading = l;
        state.events.push(`loading:${l}`);
      },
      onSuccess: (n) => state.events.push(`success:${n}`),
      onEmpty: () => state.events.push("empty"),
      onError: (msg) => state.events.push(`error:${msg}`),
    }
  );
  return {
    coordinator,
    fetches,
    state,
    setIdentity: (next: TEndpointIdentity) => {
      identity = next;
    },
  };
};

const B_IDENTITY: TEndpointIdentity = { base: "https://b.example/v1", key: "key-B" };

describe("model-list coordinator (RC 3019 async regression)", () => {
  it("scenario 1: identity changes to B while A is pending — list clears and loading is false", async () => {
    const h = harness();
    const loadA = h.coordinator.load();
    expect(h.state.loading).toBe(true);

    h.setIdentity(B_IDENTITY);
    h.coordinator.invalidate(); // what the subscription/onChange does, synchronously

    expect(h.state.models).toEqual([]);
    expect(h.state.loading).toBe(false); // KILL: invalidate without setLoading(false) fails here

    h.fetches[0].d.resolve(["model-a"]);
    await loadA;
    expect(h.state.loading).toBe(false);
  });

  it("scenario 2: late A resolution commits nothing — no models, no toast, no loading mutation", async () => {
    const h = harness();
    const loadA = h.coordinator.load();
    h.setIdentity(B_IDENTITY);
    h.coordinator.invalidate();

    const eventsAtInvalidate = [...h.state.events];
    h.fetches[0].d.resolve(["model-a", "model-a2"]);
    await loadA;

    // KILL: removing the generation check commits model-a here.
    expect(h.state.events).toEqual(eventsAtInvalidate);
    expect(h.state.models).toEqual([]);

    // Late REJECTION is equally silent.
    const h2 = harness();
    const loadA2 = h2.coordinator.load();
    h2.coordinator.invalidate();
    const events2 = [...h2.state.events];
    h2.fetches[0].d.reject({ error: "boom" });
    await loadA2;
    expect(h2.state.events).toEqual(events2); // no error toast from a superseded request
  });

  it("scenario 3: B starts immediately after the switch and its result commits", async () => {
    const h = harness();
    const loadA = h.coordinator.load();
    h.setIdentity(B_IDENTITY);
    h.coordinator.invalidate();

    const loadB = h.coordinator.load();
    expect(h.state.loading).toBe(true);
    expect(h.fetches[1].base).toBe(B_IDENTITY.base);
    expect(h.fetches[1].key).toBe(B_IDENTITY.key);

    // Late A lands AFTER B started — must not disturb B's pending state.
    h.fetches[0].d.resolve(["model-a"]);
    await loadA;
    expect(h.state.loading).toBe(true); // KILL: unguarded finally would clear B's spinner
    expect(h.state.models).toEqual([]);

    h.fetches[1].d.resolve(["model-b1", "model-b2"]);
    await loadB;
    expect(h.state.models).toEqual(["model-b1", "model-b2"]);
    expect(h.state.loading).toBe(false);
    expect(h.state.events).toContain("success:2");
  });

  it("scenario 4: key change invalidates BEFORE the next load (subscription ordering) — the correctly-keyed B request survives and commits", async () => {
    const h = harness();
    // The synchronous subscription fires invalidate() DURING the key change,
    // before any subsequent click can start a request…
    h.setIdentity(B_IDENTITY);
    h.coordinator.invalidate();
    // …so the load that follows is made with the new key and must NOT be killed
    // by its own identity's change. (KILL: a passive post-load invalidation —
    // the old effect shape — bumps the generation after this load starts and
    // discards the correct response.)
    const loadB = h.coordinator.load();
    expect(h.fetches[0].key).toBe("key-B");

    h.fetches[0].d.resolve(["model-b"]);
    await loadB;
    expect(h.state.models).toEqual(["model-b"]);
    expect(h.state.events).toContain("success:1");
    expect(h.state.loading).toBe(false);
  });

  it("same-identity double Load — the superseded first response cannot overwrite the newer one (generation check)", async () => {
    const h = harness();
    // Two rapid clicks against the SAME endpoint identity: only the generation
    // distinguishes them — the identity check cannot.
    const load1 = h.coordinator.load();
    const load2 = h.coordinator.load();

    // Newer request resolves first and commits…
    h.fetches[1].d.resolve(["new-model"]);
    await load2;
    expect(h.state.models).toEqual(["new-model"]);

    // …then the stale first response lands. KILL: removing the generation check
    // lets it OVERWRITE the newer result here.
    h.fetches[0].d.resolve(["old-model"]);
    await load1;
    expect(h.state.models).toEqual(["new-model"]);
    expect(h.state.loading).toBe(false);
  });

  it("identity drift without invalidate still cannot commit (belt-and-braces identity check)", async () => {
    const h = harness();
    const loadA = h.coordinator.load();
    // Identity changes but NOTHING calls invalidate — the commit-time identity
    // comparison is the last line of defense.
    h.setIdentity(B_IDENTITY);
    h.fetches[0].d.resolve(["model-a"]);
    await loadA;
    // KILL: removing the identity check commits model-a for endpoint B here.
    expect(h.state.models).toBeNull();
    // This request is still the newest, so it owns and clears the loading flag.
    expect(h.state.loading).toBe(false);
  });

  it("identity drift REJECTION is exactly as silent as drifted success (I2 symmetry)", async () => {
    // Morrow RC 3023: currentness must be one predicate for both outcomes — an
    // asymmetric catch would toast endpoint A's error into endpoint B's UI.
    const h = harness();
    const loadA = h.coordinator.load();
    h.setIdentity(B_IDENTITY); // drift, no invalidate
    h.fetches[0].d.reject({ error: "A exploded" });
    await loadA;
    // KILL: a catch that checks only the generation emits error:A exploded here.
    expect(h.state.events.filter((e) => e.startsWith("error:"))).toEqual([]);
    // Loading ownership stays generation-only: the newest request releases it.
    expect(h.state.loading).toBe(false);
  });

  it("empty and error results surface only for the current request", async () => {
    const h = harness();
    const load1 = h.coordinator.load();
    h.fetches[0].d.resolve([]);
    await load1;
    expect(h.state.events).toContain("empty");

    const load2 = h.coordinator.load();
    h.fetches[1].d.reject({ error: "denied" });
    await load2;
    expect(h.state.events).toContain("error:denied");
    expect(h.state.loading).toBe(false);
  });
});
