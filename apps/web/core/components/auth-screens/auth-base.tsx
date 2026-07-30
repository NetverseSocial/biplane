/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import React, { useState } from "react";
import { AuthRoot } from "@/components/account/auth-forms/auth-root";
import { EAuthModes } from "@/helpers/authentication.helper";
import { AuthHeader } from "./header";

type AuthBaseProps = {
  authType: EAuthModes;
};

export function AuthBase({ authType }: AuthBaseProps) {
  // biplane: AuthRoot can flip modes on error bounces — the header must follow the
  // LIVE mode or it shows "Sign up" while the user is already signing up (and vice versa).
  const [liveMode, setLiveMode] = useState<EAuthModes>(authType);
  return (
    <div className="relative z-10 flex h-screen w-screen flex-col items-center overflow-hidden overflow-y-auto px-8 pt-6 pb-10">
      <AuthHeader type={liveMode} />
      <AuthRoot authMode={authType} onAuthModeChange={setLiveMode} />
    </div>
  );
}
