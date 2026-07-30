/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { observer } from "mobx-react";
import Link from "next/link";
// icons
import { Eye, EyeOff, Info, XCircle } from "lucide-react";
// plane imports
import { API_BASE_URL, E_PASSWORD_STRENGTH, AUTH_TRACKER_ELEMENTS } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { CloseIcon } from "@plane/propel/icons";
import { Input, PasswordStrengthIndicator, Spinner } from "@plane/ui";
import { getPasswordStrength } from "@plane/utils";
// components
import { ForgotPasswordPopover } from "@/components/account/auth-forms/forgot-password-popover";
// constants
// helpers
import { EAuthModes, EAuthSteps } from "@/helpers/authentication.helper";
// services
import { AuthService } from "@/services/auth.service";

type Props = {
  email: string;
  isSMTPConfigured: boolean;
  mode: EAuthModes;
  handleEmailClear: () => void;
  handleAuthStep: (step: EAuthSteps) => void;
  nextPath: string | undefined;
};

type TPasswordFormValues = {
  email: string;
  password: string;
  confirm_password?: string;
  // biplane: collected up front on sign-up (John: name/company belong here, not
  // deferred to the onboarding wizard)
  first_name?: string;
  last_name?: string;
  company_name?: string;
};

const defaultValues: TPasswordFormValues = {
  email: "",
  password: "",
  first_name: "",
  last_name: "",
  company_name: "",
};

const authService = new AuthService();

export const AuthPasswordForm = observer(function AuthPasswordForm(props: Props) {
  const { email, isSMTPConfigured, handleAuthStep, handleEmailClear, mode, nextPath } = props;
  // plane imports
  const { t } = useTranslation();
  // ref
  const formRef = useRef<HTMLFormElement>(null);
  // states
  const [csrfPromise, setCsrfPromise] = useState<Promise<{ csrf_token: string }> | undefined>(undefined);
  const [passwordFormData, setPasswordFormData] = useState<TPasswordFormValues>({ ...defaultValues, email });
  const [showPassword, setShowPassword] = useState({
    password: false,
    retypePassword: false,
  });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPasswordInputFocused, setIsPasswordInputFocused] = useState(false);
  const [isRetryPasswordInputFocused, setIsRetryPasswordInputFocused] = useState(false);
  const [isBannerMessage, setBannerMessage] = useState(false);
  // biplane: strength is a warning, not a wall — after the banner, the user may
  // explicitly proceed with their password (mirrors instance setup). The SERVER
  // (zxcvbn) is the authority: a PASSWORD_TOO_WEAK bounce must surface the banner
  // and checkbox even when the frontend heuristic thought the password was fine
  // (witness: Password1! — frontend-valid, zxcvbn score 1).
  const [acceptWeakPassword, setAcceptWeakPassword] = useState(false);
  const bounceParams = useSearchParams();
  useEffect(() => {
    if (mode === EAuthModes.SIGN_UP && bounceParams?.get("error_message") === "PASSWORD_TOO_WEAK")
      setBannerMessage(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleShowPassword = (key: keyof typeof showPassword) =>
    setShowPassword((prev) => ({ ...prev, [key]: !prev[key] }));

  const handleFormChange = (key: keyof TPasswordFormValues, value: string) =>
    setPasswordFormData((prev) => ({ ...prev, [key]: value }));

  useEffect(() => {
    if (csrfPromise === undefined) {
      const promise = authService.requestCSRFToken();
      setCsrfPromise(promise);
    }
  }, [csrfPromise]);

  const redirectToUniqueCodeSignIn = async () => {
    handleAuthStep(EAuthSteps.UNIQUE_CODE);
  };

  const passwordSupport =
    mode === EAuthModes.SIGN_IN ? (
      <div className="w-full">
        {isSMTPConfigured ? (
          <Link
            data-ph-element={AUTH_TRACKER_ELEMENTS.FORGOT_PASSWORD_FROM_SIGNIN}
            href={`/accounts/forgot-password?email=${encodeURIComponent(email)}`}
            className="text-11 font-medium text-accent-primary"
          >
            {t("auth.common.forgot_password")}
          </Link>
        ) : (
          <ForgotPasswordPopover />
        )}
      </div>
    ) : (
      passwordFormData.password.length > 0 &&
      getPasswordStrength(passwordFormData.password) != E_PASSWORD_STRENGTH.STRENGTH_VALID && (
        <PasswordStrengthIndicator password={passwordFormData.password} isFocused={isPasswordInputFocused} />
      )
    );

  const isButtonDisabled = useMemo(
    () =>
      !isSubmitting &&
      !!passwordFormData.password &&
      (mode === EAuthModes.SIGN_UP
        ? passwordFormData.password === passwordFormData.confirm_password && !!passwordFormData.first_name?.trim()
        : true)
        ? false
        : true,
    [isSubmitting, mode, passwordFormData.confirm_password, passwordFormData.password, passwordFormData.first_name]
  );

  const password = passwordFormData?.password ?? "";
  const confirmPassword = passwordFormData?.confirm_password ?? "";
  const renderPasswordMatchError = !isRetryPasswordInputFocused || confirmPassword.length >= password.length;

  const handleCSRFToken = async () => {
    if (!formRef || !formRef.current) return;
    const token = await csrfPromise;
    if (!token?.csrf_token) return;
    const csrfElement = formRef.current.querySelector("input[name=csrfmiddlewaretoken]");
    csrfElement?.setAttribute("value", token?.csrf_token);
  };

  return (
    <>
      {isBannerMessage && mode === EAuthModes.SIGN_UP && (
        <div className="space-y-2 rounded-md border border-danger-strong/50 bg-danger-subtle p-2">
          <div className="relative flex items-center gap-2">
            <div className="relative flex h-4 w-4 shrink-0 items-center justify-center">
              <Info size={16} className="text-danger-primary" />
            </div>
            <div className="w-full text-13 font-medium text-danger-primary">
              {t("auth.sign_up.errors.password.strength")}
            </div>
            <button
              type="button"
              className="relative ml-auto flex h-6 w-6 cursor-pointer items-center justify-center rounded-xs text-accent-primary/80 transition-all hover:bg-danger-subtle-hover"
              onClick={() => setBannerMessage(false)}
            >
              <CloseIcon className="h-4 w-4 shrink-0 text-danger-primary" />
            </button>
          </div>
          {/* biplane: explicit override — strength warns, the user decides */}
          <label className="flex cursor-pointer items-center gap-2 pl-6 text-13 text-tertiary">
            <input
              type="checkbox"
              checked={acceptWeakPassword}
              onChange={() => setAcceptWeakPassword((prev) => !prev)}
            />
            Use this password anyway — I understand it may be easy to guess
          </label>
        </div>
      )}
      <form
        ref={formRef}
        className="space-y-4"
        method="POST"
        action={`${API_BASE_URL}/auth/${mode === EAuthModes.SIGN_IN ? "sign-in" : "sign-up"}/`}
        onSubmit={async (event) => {
          event.preventDefault(); // Prevent form from submitting by default
          await handleCSRFToken();
          // biplane: the strength gate yields to the explicit override checkbox.
          const isPasswordValid =
            mode === EAuthModes.SIGN_UP
              ? acceptWeakPassword ||
                getPasswordStrength(passwordFormData.password) === E_PASSWORD_STRENGTH.STRENGTH_VALID
              : true;
          if (isPasswordValid) {
            // biplane: hand the company name to the onboarding workspace step —
            // the sign-up POST redirects, so sessionStorage is the carrier.
            if (mode === EAuthModes.SIGN_IN) {
              // A sign-in ends any pending sign-up handoff — clear the carrier so
              // an abandoned sign-up's company can never linger into a later flow.
              sessionStorage.removeItem("bp_company_name");
            }
            if (mode === EAuthModes.SIGN_UP) {
              // Overwrite on EVERY submit: a blank company on a retry must clear the
              // previous attempt's value, never leave it stale (Morrow RC 3028).
              const company = passwordFormData.company_name?.trim();
              if (company)
                sessionStorage.setItem("bp_company_name", JSON.stringify({ email: passwordFormData.email, company }));
              else sessionStorage.removeItem("bp_company_name");
            }
            setIsSubmitting(true);
            if (formRef.current) formRef.current.submit(); // Manually submit the form if the condition is met
          } else {
            setBannerMessage(true);
          }
        }}
        onError={() => {
          setIsSubmitting(false);
        }}
      >
        <input type="hidden" name="csrfmiddlewaretoken" />
        <input type="hidden" name="user_timezone" value={Intl.DateTimeFormat().resolvedOptions().timeZone} />
        <input type="hidden" value={passwordFormData.email} name="email" />
        {nextPath && <input type="hidden" value={nextPath} name="next_path" />}
        {mode === EAuthModes.SIGN_UP && acceptWeakPassword && (
          <input type="hidden" name="accept_weak_password" value="True" />
        )}
        {mode === EAuthModes.SIGN_UP && (
          <div className="flex flex-col gap-4 sm:flex-row">
            <div className="w-full space-y-1">
              <label htmlFor="first_name" className="text-13 font-medium text-tertiary">
                First name <span className="text-danger-primary">*</span>
              </label>
              <Input
                id="first_name"
                name="first_name"
                type="text"
                value={passwordFormData.first_name}
                onChange={(e) => handleFormChange("first_name", e.target.value)}
                placeholder="Amelia"
                className="h-10 w-full border border-strong !bg-surface-1 placeholder:text-placeholder"
                maxLength={50}
              />
            </div>
            <div className="w-full space-y-1">
              <label htmlFor="last_name" className="text-13 font-medium text-tertiary">
                Last name
              </label>
              <Input
                id="last_name"
                name="last_name"
                type="text"
                value={passwordFormData.last_name}
                onChange={(e) => handleFormChange("last_name", e.target.value)}
                placeholder="Earhart"
                className="h-10 w-full border border-strong !bg-surface-1 placeholder:text-placeholder"
                maxLength={50}
              />
            </div>
          </div>
        )}
        {mode === EAuthModes.SIGN_UP && (
          <div className="space-y-1">
            <label htmlFor="company_name" className="text-13 font-medium text-tertiary">
              Company name
            </label>
            <Input
              id="company_name"
              type="text"
              value={passwordFormData.company_name}
              onChange={(e) => handleFormChange("company_name", e.target.value)}
              placeholder="Prefills your workspace"
              className="h-10 w-full border border-strong !bg-surface-1 placeholder:text-placeholder"
              maxLength={80}
            />
          </div>
        )}
        <div className="space-y-1">
          <label htmlFor="email" className="text-13 font-medium text-tertiary">
            {t("auth.common.email.label")}
          </label>
          <div className={`relative flex items-center rounded-md border border-strong bg-surface-1`}>
            <Input
              id="email"
              name="email"
              type="email"
              value={passwordFormData.email}
              onChange={(e) => handleFormChange("email", e.target.value)}
              placeholder={t("auth.common.email.placeholder")}
              className={`h-10 w-full border-0 disable-autofill-style placeholder:text-placeholder`}
              disabled
            />
            {passwordFormData.email.length > 0 && (
              <button
                type="button"
                className="absolute right-3 size-5"
                onClick={handleEmailClear}
                aria-label={t("aria_labels.auth_forms.clear_email")}
              >
                <XCircle className="size-5 stroke-placeholder" />
              </button>
            )}
          </div>
        </div>

        <div className="space-y-1">
          <label htmlFor="password" className="text-13 font-medium text-tertiary">
            {mode === EAuthModes.SIGN_IN ? t("auth.common.password.label") : t("auth.common.password.set_password")}
          </label>
          <div className="relative flex items-center rounded-md bg-surface-1">
            <Input
              type={showPassword?.password ? "text" : "password"}
              id="password"
              name="password"
              value={passwordFormData.password}
              onChange={(e) => handleFormChange("password", e.target.value)}
              placeholder={t("auth.common.password.placeholder")}
              className="h-10 w-full border border-strong !bg-surface-1 pr-12 disable-autofill-style placeholder:text-placeholder"
              onFocus={() => setIsPasswordInputFocused(true)}
              onBlur={() => setIsPasswordInputFocused(false)}
              autoComplete="off"
              autoFocus
            />
            <button
              type="button"
              onClick={() => handleShowPassword("password")}
              className="absolute right-3 grid size-5 place-items-center"
              aria-label={t(
                showPassword?.password ? "aria_labels.auth_forms.hide_password" : "aria_labels.auth_forms.show_password"
              )}
            >
              {showPassword?.password ? (
                <EyeOff className="size-5 stroke-placeholder" />
              ) : (
                <Eye className="size-5 stroke-placeholder" />
              )}
            </button>
          </div>
          {passwordSupport}
        </div>

        {mode === EAuthModes.SIGN_UP && (
          <div className="space-y-1">
            <label htmlFor="confirm-password" className="text-13 font-medium text-tertiary">
              {t("auth.common.password.confirm_password.label")}
            </label>
            <div className="relative flex items-center rounded-md bg-surface-1">
              <Input
                type={showPassword?.retypePassword ? "text" : "password"}
                id="confirm-password"
                name="confirm_password"
                value={passwordFormData.confirm_password}
                onChange={(e) => handleFormChange("confirm_password", e.target.value)}
                placeholder={t("auth.common.password.confirm_password.placeholder")}
                className="h-10 w-full border border-strong !bg-surface-1 pr-12 disable-autofill-style placeholder:text-placeholder"
                onFocus={() => setIsRetryPasswordInputFocused(true)}
                onBlur={() => setIsRetryPasswordInputFocused(false)}
                autoComplete="off"
              />
              <button
                type="button"
                className="absolute right-3 grid size-5 place-items-center"
                aria-label={t(
                  showPassword?.retypePassword
                    ? "aria_labels.auth_forms.hide_password"
                    : "aria_labels.auth_forms.show_password"
                )}
                onClick={() => handleShowPassword("retypePassword")}
              >
                {showPassword?.retypePassword ? (
                  <EyeOff className="size-5 stroke-placeholder" />
                ) : (
                  <Eye className="size-5 stroke-placeholder" />
                )}
              </button>
            </div>
            {!!passwordFormData.confirm_password &&
              passwordFormData.password !== passwordFormData.confirm_password &&
              renderPasswordMatchError && (
                <span className="text-13 text-danger-primary">{t("auth.common.password.errors.match")}</span>
              )}
          </div>
        )}

        <div className="space-y-2.5">
          {mode === EAuthModes.SIGN_IN ? (
            <>
              <Button type="submit" variant="primary" className="w-full" size="xl" disabled={isButtonDisabled}>
                {isSubmitting ? (
                  <Spinner height="20px" width="20px" />
                ) : isSMTPConfigured ? (
                  t("common.continue")
                ) : (
                  t("common.go_to_workspace")
                )}
              </Button>
              {isSMTPConfigured && (
                <Button
                  type="button"
                  data-ph-element={AUTH_TRACKER_ELEMENTS.SIGN_IN_WITH_UNIQUE_CODE}
                  onClick={redirectToUniqueCodeSignIn}
                  variant="secondary"
                  className="w-full"
                  size="xl"
                >
                  {t("auth.common.sign_in_with_unique_code")}
                </Button>
              )}
            </>
          ) : (
            <Button type="submit" variant="primary" className="w-full" size="xl" disabled={isButtonDisabled}>
              {isSubmitting ? <Spinner height="20px" width="20px" /> : "Create account"}
            </Button>
          )}
        </div>
      </form>
    </>
  );
});
