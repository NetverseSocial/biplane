/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: the loading splash shows OUR logo — the ascending biplane with a gentle climb-and-bob
// animation. Inline SVG in currentColor, so it follows the theme with no gif assets at all.
import { BiplaneLogo } from "@/app/(all)/[workspaceSlug]/(projects)/biplane-logo";

export function LogoSpinner() {
  return (
    <div className="flex items-center justify-center text-primary" aria-label="biplane is loading">
      <style>{`
        @keyframes bp-climb {
          0%   { transform: translate(-6px, 5px); opacity: .75; }
          50%  { transform: translate(0px, -2px); opacity: 1; }
          100% { transform: translate(6px, 5px); opacity: .75; }
        }
      `}</style>
      <span style={{ display: "inline-block", animation: "bp-climb 1.6s ease-in-out infinite alternate" }}>
        <BiplaneLogo size={44} />
      </span>
    </div>
  );
}
