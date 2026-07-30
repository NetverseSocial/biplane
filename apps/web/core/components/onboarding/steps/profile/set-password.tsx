/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import React, { useState, useCallback, useMemo, useEffect } from "react";
import { E_PASSWORD_STRENGTH } from "@plane/constants";
import { getPasswordStrength } from "@plane/utils";
import { LockIcon, ChevronDownIcon } from "@plane/propel/icons";
import { PasswordInput, PasswordStrengthIndicator } from "@plane/ui";
import { cn } from "@plane/utils";

interface PasswordState {
  password: string;
  confirmPassword: string;
}

interface SetPasswordRootProps {
  onPasswordChange?: (password: string) => void;
  onConfirmPasswordChange?: (confirmPassword: string) => void;
  disabled?: boolean;
  // biplane: strength warns, the user decides — this component is the PRODUCER for
  // the parent's override state (Morrow RC 3035 / Sable RC 3036: the state existed
  // with no control that could set it, so the branch and payload were dead).
  showWeakPasswordOverride?: boolean;
  acceptWeakPassword?: boolean;
  onAcceptWeakPasswordChange?: (accept: boolean) => void;
}

export function SetPasswordRoot({
  onPasswordChange,
  onConfirmPasswordChange,
  disabled = false,
  showWeakPasswordOverride = false,
  acceptWeakPassword = false,
  onAcceptWeakPasswordChange,
}: SetPasswordRootProps) {
  // biplane (John, prod testing): fields are VISIBLE by default — consistent with the
  // sign-up door; no click-to-reveal for something the user is being asked to do.
  const [isExpanded, setIsExpanded] = useState(true);
  const [passwordState, setPasswordState] = useState<PasswordState>({
    password: "",
    confirmPassword: "",
  });

  // biplane: the override checkbox visibility is decided HERE because this
  // component owns the typed value. The parent previously gated it on
  // watch("password") — a field the child never registers — so the control never
  // rendered at all. Caught by an executable browser walk; invisible to source
  // assertions.
  const isTypedPasswordWeak = useMemo(
    () =>
      passwordState.password.length > 0 &&
      getPasswordStrength(passwordState.password) !== E_PASSWORD_STRENGTH.STRENGTH_VALID,
    [passwordState.password]
  );

  // A server rejection must never land in a collapsed section — the checkbox the
  // user has to act on lives here. Force the section open when the verdict arrives.
  useEffect(() => {
    if (showWeakPasswordOverride) setIsExpanded(true);
  }, [showWeakPasswordOverride]);

  const handleToggleExpand = useCallback(() => {
    if (disabled) return;
    setIsExpanded((prev) => !prev);
  }, [disabled]);

  const handlePasswordChange = useCallback(
    (field: keyof PasswordState, value: string) => {
      setPasswordState((prev) => {
        const newState = { ...prev, [field]: value };

        // Notify parent component when password changes
        if (field === "password" && onPasswordChange) {
          onPasswordChange(value);
        }
        if (field === "confirmPassword" && onConfirmPasswordChange) {
          onConfirmPasswordChange(value);
        }

        return newState;
      });
    },
    [onPasswordChange, onConfirmPasswordChange]
  );

  const isPasswordValid = useMemo(() => {
    const { password, confirmPassword } = passwordState;
    return password.length >= 8 && password === confirmPassword;
  }, [passwordState]);

  const hasPasswordMismatch = useMemo(() => {
    const { password, confirmPassword } = passwordState;
    return confirmPassword.length > 0 && password !== confirmPassword;
  }, [passwordState]);

  const chevronIconClasses = useMemo(
    () =>
      `w-4 h-4 text-placeholder transition-transform duration-300 ease-in-out ${isExpanded ? "rotate-180" : "rotate-0"}`,
    [isExpanded]
  );

  const expandedContentClasses = useMemo(
    () =>
      `flex flex-col gap-4 transition-all duration-300 ease-in-out overflow-hidden px-3 ${
        isExpanded ? "max-h-96 opacity-100" : "max-h-0 opacity-0"
      }`,
    [isExpanded]
  );

  return (
    <div className={`flex flex-col overflow-hidden rounded-lg bg-surface-2 transition-all duration-300 ease-in-out`}>
      <div
        className={cn(
          "flex items-center justify-between px-3 py-2 text-13 transition-colors duration-200",
          disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
          isExpanded && "pb-1"
        )}
        onClick={handleToggleExpand}
      >
        <div className="flex items-center gap-1 text-tertiary">
          <LockIcon className="size-3" />
          <span className="font-medium">Set a password</span>
          <span>{`(Optional)`}</span>
        </div>
        <div className="flex items-center gap-2 text-placeholder">
          <ChevronDownIcon className={chevronIconClasses} />
        </div>
      </div>

      <div className={expandedContentClasses}>
        {(showWeakPasswordOverride || isTypedPasswordWeak) && onAcceptWeakPasswordChange && (
          <label className="flex cursor-pointer items-center gap-2 px-3 pt-2 text-11 text-tertiary">
            <input
              type="checkbox"
              checked={acceptWeakPassword}
              onChange={() => onAcceptWeakPasswordChange(!acceptWeakPassword)}
            />
            Use this password anyway — I understand it may be easy to guess
          </label>
        )}
        {/* Password input */}
        <div className="flex transform flex-col gap-2 pt-1 transition-all duration-300 ease-in-out">
          <PasswordInput
            id="password"
            value={passwordState.password}
            onChange={(value) => handlePasswordChange("password", value)}
            placeholder="Set a password"
            className="transition-all duration-200"
          />
          {passwordState.password.length > 0 && <PasswordStrengthIndicator password={passwordState.password} />}
        </div>

        <div className="flex flex-col gap-2 pb-2">
          {/* Confirm password label */}
          <div className="transform text-13 font-medium text-tertiary transition-all delay-75 duration-300 ease-in-out">
            Confirm password
          </div>

          {/* Confirm password input */}
          <div className="transform transition-all delay-100 duration-300 ease-in-out">
            <PasswordInput
              id="confirm-password"
              value={passwordState.confirmPassword}
              onChange={(value) => handlePasswordChange("confirmPassword", value)}
              placeholder="Confirm password"
              className="transition-all duration-200"
            />
            {hasPasswordMismatch && <p className="mt-1 text-11 text-danger-primary">Passwords do not match</p>}
            {isPasswordValid && <p className="mt-1 text-11 text-success-primary">✓ Passwords match</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
