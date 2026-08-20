/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// assets
import packageJson from "package.json";

export function PlaneVersionNumber() {
  // biplane (BIP-30, sharpened 2026-08-16): OUR release version leads, and
  // upstream's base version no longer wears the word "Version". The old line
  // read "Biplane 4f54555 · Version: v1.3.1" — and v1.3.1 is the UPSTREAM
  // Plane base, so a correctly-deployed v1.1.0 board looked wrong three
  // separate times to the person checking it. The release tag is baked at
  // image build time (VITE_BIPLANE_VERSION, from BIPLANE_RELEASE_TAG);
  // absent means a dev build and the line says so instead of borrowing a
  // number that isn't ours.
  const build = import.meta.env.VITE_BIPLANE_BUILD || "dev";
  const version = import.meta.env.VITE_BIPLANE_VERSION;
  return (
    <span>
      Biplane {version ? `${version} (${build})` : `dev build ${build}`}
      <span className="text-placeholder">
        {" "}
        · on Plane v{packageJson.version}
      </span>
    </span>
  );
}
