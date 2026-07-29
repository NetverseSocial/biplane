/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
// icons
import { Eye, EyeOff } from "lucide-react";
// plane internal packages
import { API_BASE_URL, E_PASSWORD_STRENGTH } from "@plane/constants";
import { Button } from "@plane/propel/button";
import { AuthService } from "@plane/services";
import { Checkbox, Input, PasswordStrengthIndicator, Spinner } from "@plane/ui";
import { getPasswordStrength, validatePersonName, validateCompanyName } from "@plane/utils";
// components
import { AuthHeader } from "@/app/(all)/(home)/auth-header";
import { Banner } from "../common/banner";
import { FormHeader } from "./form-header";

// service initialization
const authService = new AuthService();

// error codes
enum EErrorCodes {
  INSTANCE_NOT_CONFIGURED = "INSTANCE_NOT_CONFIGURED",
  ADMIN_ALREADY_EXIST = "ADMIN_ALREADY_EXIST",
  REQUIRED_EMAIL_PASSWORD_FIRST_NAME = "REQUIRED_EMAIL_PASSWORD_FIRST_NAME",
  INVALID_EMAIL = "INVALID_EMAIL",
  INVALID_PASSWORD = "INVALID_PASSWORD",
  USER_ALREADY_EXISTS = "USER_ALREADY_EXISTS",
  PASSWORD_TOO_WEAK = "PASSWORD_TOO_WEAK",
}

type TError = {
  type: EErrorCodes | undefined;
  message: string | undefined;
};

// form data
type TFormData = {
  first_name: string;
  last_name: string;
  email: string;
  company_name: string;
  password: string;
  confirm_password?: string;
  is_telemetry_enabled: boolean;
};

const defaultFromData: TFormData = {
  first_name: "",
  last_name: "",
  email: "",
  company_name: "",
  password: "",
  is_telemetry_enabled: false, // biplane: telemetry hard-off — nothing leaves your server
};

export function InstanceSetupForm() {
  // search params
  const searchParams = useSearchParams();
  const firstNameParam = searchParams?.get("first_name") || undefined;
  const lastNameParam = searchParams?.get("last_name") || undefined;
  const companyParam = searchParams?.get("company") || undefined;
  const emailParam = searchParams?.get("email") || undefined;
  const isTelemetryEnabledParam = false; // biplane: telemetry hard-off
  const errorCode = searchParams?.get("error_code") || undefined;
  const errorMessage = searchParams?.get("error_message") || undefined;
  const passwordFeedback = searchParams?.get("password_feedback") || undefined;
  // state
  const [showPassword, setShowPassword] = useState({
    password: false,
    retypePassword: false,
  });
  const [csrfToken, setCsrfToken] = useState<string | undefined>(undefined);
  const [formData, setFormData] = useState<TFormData>(defaultFromData);
  const [isPasswordInputFocused, setIsPasswordInputFocused] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isRetryPasswordInputFocused, setIsRetryPasswordInputFocused] = useState(false);
  // biplane: strength is a warning, not a wall — after a PASSWORD_TOO_WEAK bounce the
  // operator can opt to proceed with the same password (John's call, 2026-07-27)
  const [acceptWeakPassword, setAcceptWeakPassword] = useState(false);

  const handleShowPassword = (key: keyof typeof showPassword) =>
    setShowPassword((prev) => ({ ...prev, [key]: !prev[key] }));

  const handleFormChange = (key: keyof TFormData, value: string | boolean) =>
    setFormData((prev) => ({ ...prev, [key]: value }));

  useEffect(() => {
    if (csrfToken === undefined)
      authService.requestCSRFToken().then((data) => data?.csrf_token && setCsrfToken(data.csrf_token));
  }, [csrfToken]);

  useEffect(() => {
    if (firstNameParam) setFormData((prev) => ({ ...prev, first_name: firstNameParam }));
    if (lastNameParam) setFormData((prev) => ({ ...prev, last_name: lastNameParam }));
    if (companyParam) setFormData((prev) => ({ ...prev, company_name: companyParam }));
    if (emailParam) setFormData((prev) => ({ ...prev, email: emailParam }));
    if (isTelemetryEnabledParam) setFormData((prev) => ({ ...prev, is_telemetry_enabled: isTelemetryEnabledParam }));
  }, [firstNameParam, lastNameParam, companyParam, emailParam, isTelemetryEnabledParam]);

  // biplane: a weak-password bounce must not make the operator re-type both password
  // fields — stash on submit (sessionStorage, this tab only), restore once on the
  // bounce, clear immediately either way.
  useEffect(() => {
    const stashed = sessionStorage.getItem("bp_setup_pw");
    sessionStorage.removeItem("bp_setup_pw");
    if (stashed && errorMessage === EErrorCodes.PASSWORD_TOO_WEAK) {
      setFormData((prev) => ({ ...prev, password: stashed, confirm_password: stashed }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // derived values
  const errorData: TError = useMemo(() => {
    if (errorCode && errorMessage) {
      // biplane: the backend redirects with error_code=<numeric> and error_message=<name>;
      // this switch previously compared the NUMERIC code against the NAME enum, so no
      // error ever matched and every failure rendered as a silent blank reload.
      switch (errorMessage) {
        case EErrorCodes.INSTANCE_NOT_CONFIGURED:
          return { type: EErrorCodes.INSTANCE_NOT_CONFIGURED, message: errorMessage };
        case EErrorCodes.ADMIN_ALREADY_EXIST:
          return { type: EErrorCodes.ADMIN_ALREADY_EXIST, message: errorMessage };
        case EErrorCodes.REQUIRED_EMAIL_PASSWORD_FIRST_NAME:
          return { type: EErrorCodes.REQUIRED_EMAIL_PASSWORD_FIRST_NAME, message: errorMessage };
        case EErrorCodes.INVALID_EMAIL:
          return { type: EErrorCodes.INVALID_EMAIL, message: errorMessage };
        case EErrorCodes.INVALID_PASSWORD:
          return { type: EErrorCodes.INVALID_PASSWORD, message: errorMessage };
        case EErrorCodes.USER_ALREADY_EXISTS:
          return { type: EErrorCodes.USER_ALREADY_EXISTS, message: errorMessage };
        case EErrorCodes.PASSWORD_TOO_WEAK:
          return {
            type: EErrorCodes.PASSWORD_TOO_WEAK,
            message: `Password looks easy to guess: ${passwordFeedback || "it matches common patterns."} Re-enter it and pick a stronger one, or check "Use this password anyway".`,
          };
        default:
          // Unknown codes must still surface — never silently swallow an error again.
          return { type: undefined, message: `${errorMessage.replaceAll("_", " ").toLowerCase()} (${errorCode})` };
      }
    } else return { type: undefined, message: undefined };
  }, [errorCode, errorMessage, passwordFeedback]);

  const isButtonDisabled = useMemo(
    () =>
      !isSubmitting &&
      formData.first_name &&
      formData.email &&
      formData.password &&
      // biplane: the weak-password override must actually unlock the button —
      // strength gates submission only while the override is unchecked.
      (acceptWeakPassword || getPasswordStrength(formData.password) === E_PASSWORD_STRENGTH.STRENGTH_VALID) &&
      formData.password === formData.confirm_password
        ? false
        : true,
    [
      acceptWeakPassword,
      formData.confirm_password,
      formData.email,
      formData.first_name,
      formData.password,
      isSubmitting,
    ]
  );

  const password = formData?.password ?? "";
  const confirmPassword = formData?.confirm_password ?? "";
  const renderPasswordMatchError = !isRetryPasswordInputFocused || confirmPassword.length >= password.length;

  return (
    <>
      <AuthHeader />
      <div className="mt-10 flex w-full flex-grow flex-col items-center justify-center py-6">
        <div className="relative flex w-full max-w-[22.5rem] flex-col gap-6">
          <FormHeader
            heading="Set up your Biplane instance"
            subHeading="Post setup you will be able to manage this Biplane instance."
          />
          {errorData?.message &&
            (!errorData.type ||
              ![EErrorCodes.INVALID_EMAIL, EErrorCodes.INVALID_PASSWORD].includes(errorData.type)) && (
              <Banner type="error" message={errorData?.message} />
            )}
          <form
            className="space-y-4"
            method="POST"
            action={`${API_BASE_URL}/api/instances/admins/sign-up/`}
            onSubmit={() => {
              sessionStorage.setItem("bp_setup_pw", formData.password);
              setIsSubmitting(true);
            }}
            onError={() => setIsSubmitting(false)}
          >
            <input type="hidden" name="csrfmiddlewaretoken" value={csrfToken} />
            <input type="hidden" name="is_telemetry_enabled" value={formData.is_telemetry_enabled ? "True" : "False"} />
            {acceptWeakPassword && <input type="hidden" name="accept_weak_password" value="True" />}

            <div className="flex flex-col items-center gap-4 sm:flex-row">
              <div className="w-full space-y-1">
                <label className="text-13 font-medium text-tertiary" htmlFor="first_name">
                  First name <span className="text-danger-primary">*</span>
                </label>
                <Input
                  className="w-full border border-subtle !bg-surface-1 placeholder:text-placeholder"
                  id="first_name"
                  name="first_name"
                  type="text"
                  inputSize="md"
                  placeholder="Wilber"
                  value={formData.first_name}
                  onChange={(e) => {
                    const validation = validatePersonName(e.target.value);
                    if (validation === true || e.target.value === "") {
                      handleFormChange("first_name", e.target.value);
                    }
                  }}
                  autoComplete="off"
                  autoFocus
                  maxLength={50}
                />
              </div>
              <div className="w-full space-y-1">
                <label className="text-13 font-medium text-tertiary" htmlFor="last_name">
                  Last name
                </label>
                <Input
                  className="w-full border border-subtle !bg-surface-1 placeholder:text-placeholder"
                  id="last_name"
                  name="last_name"
                  type="text"
                  inputSize="md"
                  placeholder="Wright"
                  value={formData.last_name}
                  onChange={(e) => {
                    const validation = validatePersonName(e.target.value);
                    if (validation === true || e.target.value === "") {
                      handleFormChange("last_name", e.target.value);
                    }
                  }}
                  autoComplete="off"
                  maxLength={50}
                />
              </div>
            </div>

            <div className="w-full space-y-1">
              <label className="text-13 font-medium text-tertiary" htmlFor="email">
                Email <span className="text-danger-primary">*</span>
              </label>
              <Input
                className="w-full border border-subtle !bg-surface-1 placeholder:text-placeholder"
                id="email"
                name="email"
                type="email"
                inputSize="md"
                placeholder="name@company.com"
                value={formData.email}
                onChange={(e) => handleFormChange("email", e.target.value)}
                hasError={errorData.type && errorData.type === EErrorCodes.INVALID_EMAIL ? true : false}
                autoComplete="off"
              />
              {errorData.type && errorData.type === EErrorCodes.INVALID_EMAIL && errorData.message && (
                <p className="px-1 text-11 text-danger-primary">{errorData.message}</p>
              )}
            </div>

            <div className="w-full space-y-1">
              <label className="text-13 font-medium text-tertiary" htmlFor="company_name">
                Company name
              </label>
              <Input
                className="w-full border border-subtle !bg-surface-1 placeholder:text-placeholder"
                id="company_name"
                name="company_name"
                type="text"
                inputSize="md"
                placeholder="Company name"
                value={formData.company_name}
                onChange={(e) => {
                  const validation = validateCompanyName(e.target.value, false);
                  if (validation === true || e.target.value === "") {
                    handleFormChange("company_name", e.target.value);
                  }
                }}
                maxLength={80}
              />
            </div>

            <div className="w-full space-y-1">
              <label className="text-13 font-medium text-tertiary" htmlFor="password">
                Set a password <span className="text-danger-primary">*</span>
              </label>
              <div className="relative">
                <Input
                  className="w-full border border-subtle !bg-surface-1 placeholder:text-placeholder"
                  id="password"
                  name="password"
                  type={showPassword.password ? "text" : "password"}
                  inputSize="md"
                  placeholder="New password"
                  value={formData.password}
                  onChange={(e) => handleFormChange("password", e.target.value)}
                  hasError={errorData.type && errorData.type === EErrorCodes.INVALID_PASSWORD ? true : false}
                  onFocus={() => setIsPasswordInputFocused(true)}
                  onBlur={() => setIsPasswordInputFocused(false)}
                  autoComplete="new-password"
                />
                {showPassword.password ? (
                  <button
                    type="button"
                    tabIndex={-1}
                    className="absolute top-3.5 right-3 flex items-center justify-center text-placeholder"
                    onClick={() => handleShowPassword("password")}
                  >
                    <EyeOff className="h-4 w-4" />
                  </button>
                ) : (
                  <button
                    type="button"
                    tabIndex={-1}
                    className="absolute top-3.5 right-3 flex items-center justify-center text-placeholder"
                    onClick={() => handleShowPassword("password")}
                  >
                    <Eye className="h-4 w-4" />
                  </button>
                )}
              </div>
              {errorData.type && errorData.type === EErrorCodes.INVALID_PASSWORD && errorData.message && (
                <p className="px-1 text-11 text-danger-primary">{errorData.message}</p>
              )}
              <PasswordStrengthIndicator password={formData.password} isFocused={isPasswordInputFocused} />
            </div>

            <div className="w-full space-y-1">
              <label className="text-13 font-medium text-tertiary" htmlFor="confirm_password">
                Confirm password <span className="text-danger-primary">*</span>
              </label>
              <div className="relative">
                <Input
                  type={showPassword.retypePassword ? "text" : "password"}
                  id="confirm_password"
                  name="confirm_password"
                  inputSize="md"
                  value={formData.confirm_password}
                  onChange={(e) => handleFormChange("confirm_password", e.target.value)}
                  placeholder="Confirm password"
                  className="w-full border border-subtle !bg-surface-1 pr-12 placeholder:text-placeholder"
                  onFocus={() => setIsRetryPasswordInputFocused(true)}
                  onBlur={() => setIsRetryPasswordInputFocused(false)}
                  autoComplete="new-password"
                />
                {showPassword.retypePassword ? (
                  <button
                    type="button"
                    tabIndex={-1}
                    className="absolute top-3.5 right-3 flex items-center justify-center text-placeholder"
                    onClick={() => handleShowPassword("retypePassword")}
                  >
                    <EyeOff className="h-4 w-4" />
                  </button>
                ) : (
                  <button
                    type="button"
                    tabIndex={-1}
                    className="absolute top-3.5 right-3 flex items-center justify-center text-placeholder"
                    onClick={() => handleShowPassword("retypePassword")}
                  >
                    <Eye className="h-4 w-4" />
                  </button>
                )}
              </div>
              {!!formData.confirm_password &&
                formData.password !== formData.confirm_password &&
                renderPasswordMatchError && (
                  <span className="text-13 text-danger-primary">Passwords don{"'"}t match</span>
                )}
            </div>

            {errorData.type === EErrorCodes.PASSWORD_TOO_WEAK && (
              <label className="flex cursor-pointer items-center gap-2 text-13 text-tertiary">
                <Checkbox checked={acceptWeakPassword} onChange={() => setAcceptWeakPassword((prev) => !prev)} />
                Use this password anyway — I understand it may be easy to guess
              </label>
            )}

            {/* biplane: telemetry checkbox removed — hard-off, nothing leaves your server */}

            <div className="py-2">
              <Button type="submit" size="xl" className="w-full" disabled={isButtonDisabled}>
                {isSubmitting ? <Spinner height="20px" width="20px" /> : "Continue"}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}
