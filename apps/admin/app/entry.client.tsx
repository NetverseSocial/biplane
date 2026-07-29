/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { startTransition, StrictMode } from "react";
import { hydrateRoot } from "react-dom/client";
import { HydratedRouter } from "react-router/dom";

// biplane: the setup form stashes the password (sessionStorage) across the
// weak-password bounce. Clear it on every OTHER page load so the plaintext never
// outlives the setup flow — success redirects here without the bounce params, and
// only the bounce URL (error_message=PASSWORD_TOO_WEAK) may keep it for the form
// to restore-and-remove.
if (!window.location.search.includes("error_message=PASSWORD_TOO_WEAK")) {
  sessionStorage.removeItem("bp_setup_pw");
}

startTransition(() => {
  hydrateRoot(
    document,
    <StrictMode>
      <HydratedRouter />
    </StrictMode>
  );
});
