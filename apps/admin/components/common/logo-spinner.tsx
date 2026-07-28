/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: the loading splash shows OUR logo — same climb-and-bob animation as the web
// app. Inline SVG in currentColor follows the theme; the old Plane gif assets are gone.
import { BiplaneLogo } from "@/components/common/biplane-logo";

export function LogoSpinner() {
  return (
    <div className="flex items-center justify-center text-primary" aria-label="Biplane is loading">
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
