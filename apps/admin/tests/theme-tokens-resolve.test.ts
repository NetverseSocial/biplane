/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: a color utility that names a token the theme does not define emits
// NO CSS — the element keeps its geometry and renders transparent. That is the
// defect this guards: the apply progress gauge shipped as a correctly-sized,
// fully invisible bar (John: "I saw no gauge... JUST 5%"), and the active
// "Update to vX" button shipped as white text on a transparent background with
// a white border — invisible on a white page. Both measured in a headless
// browser against the repo's own compiled CSS before the fix.
//
// The guard is DERIVED, not asserted: premise first — the theme defines no
// `custom-*` color token — and only then does "no source file may use a
// `custom-*` color utility" follow. If someone ever adds those tokens back to
// the theme, the premise assertion fails and this rule gets re-decided rather
// than silently outliving its reason.
//
// Answerable to a mutation: restore any single replaced class (e.g.
// `bg-accent-primary` -> `bg-custom-primary-100` in the gauge) and the scan
// below reds.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const repoPath = (relative: string) => fileURLToPath(new URL(`../../../${relative}`, import.meta.url));

// Utility prefixes that resolve through a theme COLOR token. A class using one
// of these with an undefined token name produces no rule at all.
const COLOR_PREFIXES = [
  "bg",
  "text",
  "border",
  "ring",
  "fill",
  "stroke",
  "divide",
  "outline",
  "shadow",
  "placeholder",
  "accent",
  "caret",
  "decoration",
];
const CUSTOM_CLASS = new RegExp(`\\b(${COLOR_PREFIXES.join("|")})-custom-[a-z0-9-]+`, "g");

// This file names the forbidden classes in its own comments and assertions, so
// it is excluded from its own scan — the first run of this guard flagged
// exactly that, which is at least a working demonstration that the scan reads
// what it claims to.
const SELF = "theme-tokens-resolve.test.ts";

const walk = (dir: string, out: string[] = []): string[] => {
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry === ".next" || entry === "dist" || entry === "build") continue;
    if (entry === SELF) continue;
    const full = `${dir}/${entry}`;
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
};

describe("theme color tokens actually resolve", () => {
  it("the theme defines no custom-* token in ANY namespace (the premise this rule rests on)", () => {
    // Every namespace, not a hand-listed four (Sable, review 3984). v4 theme
    // variables are namespaced — `--background-color-*`, `--text-color-*`,
    // `--ring-color-*` — so checking only four while the rule below forbids
    // thirteen prefixes leaves a gap: define `--ring-color-custom-focus` and
    // the premise would still pass while `ring-custom-focus` became live and
    // legitimate, and the usage scan would red with a misleading message
    // instead of this premise failing and sending someone to re-decide.
    const variables = readFileSync(repoPath("packages/tailwind-config/variables.css"), "utf8");
    const defined = variables.match(/^\s*--[a-z-]*-custom-[a-z0-9-]+\s*:/gm);
    expect(defined).toBeNull();
  });

  it("no app source uses a custom-* color utility", () => {
    // Scope: apps/. The upstream packages/ tree still carries 8 such classes
    // (packages/ui badge+button helpers, packages/propel icon stories) — the
    // same dead-token defect, left alone deliberately: they are upstream Plane
    // files where an edit costs release-sync friction, and none is on the
    // update path this bug was reported against. Named here so the boundary is
    // stated rather than silent.
    const offenders: string[] = [];
    for (const app of ["apps/web", "apps/admin"]) {
      for (const file of walk(repoPath(app))) {
        const hits = readFileSync(file, "utf8").match(CUSTOM_CLASS);
        if (hits) offenders.push(`${file.replace(/^.*\/apps\//, "apps/")}: ${[...new Set(hits)].join(", ")}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
