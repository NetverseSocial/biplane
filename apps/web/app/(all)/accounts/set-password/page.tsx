/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// plane imports
import { EAuthModes } from "@plane/constants";
// components
// biplane (Morrow RC 3031): this page rendered ResetPasswordForm — which reads
// uidb64/token from the URL and posted /auth/reset-password/undefined/undefined/.
// The set-password flow is session-authenticated and belongs to SetPasswordForm.
import { SetPasswordForm } from "@/components/account/auth-forms/set-password";
import { AuthHeader } from "@/components/auth-screens/header";
// helpers
import { EPageTypes } from "@/helpers/authentication.helper";
// layouts
import DefaultLayout from "@/layouts/default-layout";
import { AuthenticationWrapper } from "@/lib/wrappers/authentication-wrapper";

function SetPasswordPage() {
  return (
    <DefaultLayout>
      <AuthenticationWrapper pageType={EPageTypes.SET_PASSWORD}>
        <div className="relative z-10 flex h-screen w-screen flex-col items-center overflow-hidden overflow-y-auto px-8 pt-6 pb-10">
          <AuthHeader type={EAuthModes.SIGN_IN} />
          <SetPasswordForm />
        </div>
      </AuthenticationWrapper>
    </DefaultLayout>
  );
}

export default SetPasswordPage;
