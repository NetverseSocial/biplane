/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { observer } from "mobx-react";
import { useSearchParams } from "next/navigation";
// icons
import { Eye, EyeOff } from "lucide-react";
// ui
import { API_BASE_URL, E_PASSWORD_STRENGTH } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { Input, PasswordStrengthIndicator } from "@plane/ui";
// components
import { createSubmitLock, getPasswordStrength, runResetSubmit, showsWeakOverride } from "@plane/utils";
// helpers
import type { EAuthenticationErrorCodes, TAuthErrorInfo } from "@/helpers/authentication.helper";
import { EErrorAlertType, authErrorHandler } from "@/helpers/authentication.helper";
// services
import { AuthService } from "@/services/auth.service";
// local imports
import { AuthBanner } from "./auth-banner";
import { FormContainer } from "./common/container";
import { AuthFormHeader } from "./common/header";

type TResetPasswordFormValues = {
  email: string;
  password: string;
  confirm_password?: string;
};

const defaultValues: TResetPasswordFormValues = {
  email: "",
  password: "",
};

// services
const authService = new AuthService();

export const ResetPasswordForm = observer(function ResetPasswordForm() {
  // search params
  const searchParams = useSearchParams();
  const uidb64 = searchParams.get("uidb64");
  const token = searchParams.get("token");
  // biplane: server is the strength authority — after a PASSWORD_TOO_WEAK bounce
  // (params preserved by the endpoint) the user may explicitly override.
  const bouncedWeak = searchParams.get("error_message") === "PASSWORD_TOO_WEAK";
  const [acceptWeakPassword, setAcceptWeakPassword] = useState(false);
  const email = searchParams.get("email");
  const error_code = searchParams.get("error_code");
  // states
  const [showPassword, setShowPassword] = useState({
    password: false,
    retypePassword: false,
  });
  const [resetFormData, setResetFormData] = useState<TResetPasswordFormValues>({
    ...defaultValues,
    email: email ? email.toString() : "",
  });
  // biplane (BIP-29): the reset link identifies the user by uidb64/token, so
  // email is normally prefilled and locked. But a PASSWORD_TOO_WEAK bounce
  // (and some link shapes) come back with NO email param — leaving the field
  // blank AND disabled, so the user could neither see nor type it and the form
  // was unsubmittable. When there is no prefill, the field is editable.
  const emailPrefilled = !!email;
  const [csrfToken, setCsrfToken] = useState<string | undefined>(undefined);
  const [isPasswordInputFocused, setIsPasswordInputFocused] = useState(false);
  const [isRetryPasswordInputFocused, setIsRetryPasswordInputFocused] = useState(false);
  const [errorInfo, setErrorInfo] = useState<TAuthErrorInfo | undefined>(undefined);
  // biplane (BIP-29): a weak-password bounce must NOT navigate — navigating is
  // what wiped every field. This mirrors BIP-22's sign-up fix. Set once the
  // server rejects the password as weak; drives the same banner/checkbox the
  // URL-param bounce does, but over the form the user already filled in.
  const [showWeakBanner, setShowWeakBanner] = useState(false);
  // The outcome could not be determined: the token may or may not have been
  // consumed. The user is told; nothing is retried automatically (RC 3205).
  const [isIndeterminate, setIsIndeterminate] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  // A resolved 4xx: the server REFUSED before writing, so the password was not
  // changed and saying so is safe. 5xx is NOT here — it goes to the unknown
  // banner, because the endpoint can save before it fails.
  const [serverErrorStatus, setServerErrorStatus] = useState<number | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  // Single-flight coordinator (RC 3206): the lock lives in @plane/utils where
  // tests can kill it; one instance per mount, hence the ref.
  const lockRef = useRef(createSubmitLock());
  // plane hooks
  const { t } = useTranslation();

  const handleShowPassword = (key: keyof typeof showPassword) =>
    setShowPassword((prev) => ({ ...prev, [key]: !prev[key] }));

  const handleFormChange = (key: keyof TResetPasswordFormValues, value: string) =>
    setResetFormData((prev) => ({ ...prev, [key]: value }));

  useEffect(() => {
    if (csrfToken === undefined)
      authService.requestCSRFToken().then((data) => data?.csrf_token && setCsrfToken(data.csrf_token));
  }, [csrfToken]);

  const isButtonDisabled = useMemo(
    () =>
      !!resetFormData.password &&
      (acceptWeakPassword || getPasswordStrength(resetFormData.password) === E_PASSWORD_STRENGTH.STRENGTH_VALID) &&
      resetFormData.password === resetFormData.confirm_password
        ? false
        : true,
    [resetFormData, acceptWeakPassword]
  );

  useEffect(() => {
    if (error_code) {
      const errorhandler = authErrorHandler(error_code?.toString() as EAuthenticationErrorCodes);
      if (errorhandler) {
        setErrorInfo(errorhandler);
      }
    }
  }, [error_code]);

  const password = resetFormData?.password ?? "";
  const confirmPassword = resetFormData?.confirm_password ?? "";
  const renderPasswordMatchError = !isRetryPasswordInputFocused || confirmPassword.length >= password.length;

  return (
    <FormContainer>
      <AuthFormHeader title="Reset password" description="Create a new password." />

      {errorInfo && errorInfo?.type === EErrorAlertType.BANNER_ALERT && (
        <AuthBanner message={errorInfo.message} handleBannerData={(value) => setErrorInfo(value)} />
      )}
      {(bouncedWeak || showWeakBanner) && (
        <div className="space-y-2 rounded-md border border-danger-strong/50 bg-danger-subtle p-2">
          <div className="w-full text-13 font-medium text-danger-primary">
            That password is easy to guess. Choose a stronger one, or tick the box below to use it anyway.
          </div>
        </div>
      )}
      {serverErrorStatus !== null && (
        <div className="space-y-2 rounded-md border border-danger-strong/50 bg-danger-subtle p-2">
          <div className="w-full text-13 font-medium text-danger-primary">
            The server refused the request (error {serverErrorStatus}). Your password was not changed. If this reset
            link has already been used, request a fresh one.
          </div>
        </div>
      )}
      {isIndeterminate && (
        <div className="space-y-2 rounded-md border border-danger-strong/50 bg-danger-subtle p-2">
          <div className="w-full text-13 font-medium text-danger-primary">
            We could not confirm whether your password was changed. Try signing in with the new password first — if that
            does not work, request a fresh reset link.
          </div>
        </div>
      )}
      <form
        ref={formRef}
        className="space-y-4"
        method="POST"
        action={`${API_BASE_URL}/auth/reset-password/${uidb64?.toString()}/${token?.toString()}/`}
        onSubmit={(e) => {
          const form = formRef.current;
          if (!form) return;
          // BIP-29: submit via fetch so a PASSWORD_TOO_WEAK bounce does NOT
          // navigate and wipe the form. Every other outcome follows the
          // server's own redirect exactly as the native POST did.
          e.preventDefault();
          // The ENTIRE orchestration — single-flight, status contract, no
          // native retry — lives in runResetSubmit, which the package suite
          // exercises (Morrow RC 3211). This handler supplies I/O and renders
          // outcomes; it deliberately holds no decision of its own, so a
          // regression here (a restored form.submit, a skipped guard) is
          // caught by the tests around that unit rather than being invisible.
          void runResetSubmit(
            {
              action: form.action,
              buildBody: () => new FormData(form),
              // Never invoked: present so the tests can prove no path performs
              // a native submit of a one-time token.
              submit: () => form.submit(),
            },
            {
              lock: lockRef.current,
              post: async (action, body) => {
                const r = await fetch(action, {
                  method: "POST",
                  body,
                  credentials: "include",
                  redirect: "follow",
                });
                return { url: r.url, status: r.status, redirected: r.redirected };
              },
              navigate: (url) => {
                window.location.href = url;
              },
              onPending: setIsSubmitting,
              onWeakPassword: () => setShowWeakBanner(true),
              // Resolved 4xx that did NOT redirect: the URL is the POST
              // endpoint, so navigating would GET an error page and wipe the
              // form — the very defect this ticket removes.
              onServerError: setServerErrorStatus,
              // Unknown outcome (transport failure, or a 5xx that may have
              // landed after the password was already saved): tell the user,
              // retry nothing.
              onIndeterminate: () => setIsIndeterminate(true),
            }
          );
        }}
      >
        <input type="hidden" name="csrfmiddlewaretoken" value={csrfToken} />
        {acceptWeakPassword && <input type="hidden" name="accept_weak_password" value="True" />}
        <div className="space-y-1">
          <label className="text-13 font-medium text-tertiary" htmlFor="email">
            {t("auth.common.email.label")}
          </label>
          <div className="relative flex items-center rounded-md bg-surface-1">
            <Input
              id="email"
              name="email"
              type="email"
              value={resetFormData.email}
              onChange={(e) => handleFormChange("email", e.target.value)}
              placeholder={t("auth.common.email.placeholder")}
              className={
                emailPrefilled
                  ? "h-10 w-full cursor-not-allowed border border-strong !bg-surface-1 pr-12 text-placeholder"
                  : "h-10 w-full border border-strong !bg-surface-1 pr-12 placeholder:text-placeholder"
              }
              autoComplete={emailPrefilled ? "off" : "email"}
              disabled={emailPrefilled}
              required
            />
          </div>
        </div>
        <div className="space-y-1">
          <label className="text-13 font-medium text-tertiary" htmlFor="password">
            {t("auth.common.password.label")}
          </label>
          <div className="relative flex items-center rounded-md bg-surface-1">
            <Input
              type={showPassword.password ? "text" : "password"}
              name="password"
              value={resetFormData.password}
              onChange={(e) => handleFormChange("password", e.target.value)}
              //hasError={Boolean(errors.password)}
              placeholder={t("auth.common.password.placeholder")}
              className="h-10 w-full border border-strong !bg-surface-1 pr-12 placeholder:text-placeholder"
              minLength={8}
              onFocus={() => setIsPasswordInputFocused(true)}
              onBlur={() => setIsPasswordInputFocused(false)}
              autoComplete="new-password"
              autoFocus
            />
            {showPassword.password ? (
              <EyeOff
                className="absolute right-3 h-5 w-5 stroke-placeholder hover:cursor-pointer"
                onClick={() => handleShowPassword("password")}
              />
            ) : (
              <Eye
                className="absolute right-3 h-5 w-5 stroke-placeholder hover:cursor-pointer"
                onClick={() => handleShowPassword("password")}
              />
            )}
          </div>
          <PasswordStrengthIndicator password={resetFormData.password} isFocused={isPasswordInputFocused} />
        </div>
        <div className="space-y-1">
          <label className="text-13 font-medium text-tertiary" htmlFor="confirm_password">
            {t("auth.common.password.confirm_password.label")}
          </label>
          <div className="relative flex items-center rounded-md bg-surface-1">
            <Input
              type={showPassword.retypePassword ? "text" : "password"}
              name="confirm_password"
              value={resetFormData.confirm_password}
              onChange={(e) => handleFormChange("confirm_password", e.target.value)}
              placeholder={t("auth.common.password.confirm_password.placeholder")}
              className="h-10 w-full border border-strong !bg-surface-1 pr-12 placeholder:text-placeholder"
              onFocus={() => setIsRetryPasswordInputFocused(true)}
              onBlur={() => setIsRetryPasswordInputFocused(false)}
              autoComplete="new-password"
            />
            {showPassword.retypePassword ? (
              <EyeOff
                className="absolute right-3 h-5 w-5 stroke-placeholder hover:cursor-pointer"
                onClick={() => handleShowPassword("retypePassword")}
              />
            ) : (
              <Eye
                className="absolute right-3 h-5 w-5 stroke-placeholder hover:cursor-pointer"
                onClick={() => handleShowPassword("retypePassword")}
              />
            )}
          </div>
          {!!resetFormData.confirm_password &&
            resetFormData.password !== resetFormData.confirm_password &&
            renderPasswordMatchError && (
              <span className="text-13 text-danger-primary">{t("auth.common.password.errors.match")}</span>
            )}
        </div>
        {/* The SERVER (zxcvbn) is the strength authority, and it is stricter
            than the client meter: a password the frontend calls valid can
            still be rejected. So the override must appear whenever the server
            said weak — via the URL bounce OR this session's fetch — not only
            when the client meter dislikes it (Morrow RC 3205). */}
        {showsWeakOverride({
          bouncedWeak,
          serverSaidWeak: showWeakBanner,
          clientMeterValid: getPasswordStrength(resetFormData.password) === E_PASSWORD_STRENGTH.STRENGTH_VALID,
          passwordLength: resetFormData.password.length,
          inFlight: false,
        }) && (
          <label className="flex cursor-pointer items-center gap-2 text-13 text-tertiary">
            <input
              type="checkbox"
              checked={acceptWeakPassword}
              onChange={() => setAcceptWeakPassword((prev) => !prev)}
            />
            Use this password anyway — I understand it may be easy to guess
          </label>
        )}
        <Button
          type="submit"
          variant="primary"
          className="w-full"
          size="xl"
          disabled={isButtonDisabled || isSubmitting}
        >
          {t("auth.common.password.submit")}
        </Button>
      </form>
    </FormContainer>
  );
});
