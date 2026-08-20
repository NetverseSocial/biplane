import { describe, expect, it } from "vitest";
import { EAuthErrorCodes } from "@plane/constants";
import { authErrorHandler } from "./auth";

/**
 * BIP-1 step 1 — behaviour parity gate for the codes being migrated.
 *
 * Two copies of `authErrorHandler` exist: this one and the one in
 * apps/web/helpers/authentication.helper.tsx. Step 2 will delete the web copy
 * and re-export this one. That is only safe if this copy produces the same
 * visible output the web copy produces TODAY, for every code involved.
 *
 * The first version of this file asserted reachability and two titles. It went
 * 9/9 green while four of these codes returned different MESSAGES from the web
 * copy — any message rewrite stayed invisible. Both reviewers caught it. So the
 * table below pins the exact TITLE and MESSAGE, and the expected values are the
 * web copy's observed return values, transcribed from authentication.helper.tsx
 * rather than from this file.
 *
 * One subtlety that has to be got right rather than guessed. The web copy stores
 * `title: ""` for RATE_LIMIT_EXCEEDED, and both handlers end with
 * `title || "Error"`. `"" || "Error"` is `"Error"`, so the title a user actually
 * sees is "Error", NOT the empty string. Preserving the empty title with a
 * nullish fallback (`?? "Error"`) would return `""` and therefore BREAK parity
 * rather than keep it. Parity is on what the function returns.
 */

type Expected = { title: string; message: string };

/** Transcribed from apps/web/helpers/authentication.helper.tsx. */
const WEB_CONTRACT: Array<{ code: EAuthErrorCodes; label: string; expected: Expected }> = [
  {
    code: EAuthErrorCodes.MAGIC_LINK_LOGIN_DISABLED,
    label: "MAGIC_LINK_LOGIN_DISABLED",
    expected: {
      title: "Magic link login disabled",
      message: "Magic link login disabled. Please contact your administrator.",
    },
  },
  {
    code: EAuthErrorCodes.PASSWORD_LOGIN_DISABLED,
    label: "PASSWORD_LOGIN_DISABLED",
    expected: {
      title: "Password login disabled",
      message: "Password login disabled. Please contact your administrator.",
    },
  },
  {
    code: EAuthErrorCodes.ADMIN_USER_DEACTIVATED,
    label: "ADMIN_USER_DEACTIVATED",
    // The web copy returns JSX here (<div>Your account is deactivated</div>).
    // This package is platform-neutral and returns strings, so parity is on the
    // rendered text. That representation difference is real: step 2 must convert
    // the consumer, not silently drop the wrapper.
    expected: { title: "Admin user deactivated", message: "Your account is deactivated" },
  },
  {
    code: EAuthErrorCodes.RATE_LIMIT_EXCEEDED,
    label: "RATE_LIMIT_EXCEEDED",
    expected: { title: "Error", message: "Rate limit exceeded. Please try again later." },
  },
  {
    code: EAuthErrorCodes.MISSING_PASSWORD,
    label: "MISSING_PASSWORD",
    expected: { title: "Password required", message: "Password required. Please try again." },
  },
  {
    code: EAuthErrorCodes.REQUIRED_FIRST_NAME_SIGN_UP,
    label: "REQUIRED_FIRST_NAME_SIGN_UP",
    expected: {
      title: "First name required",
      message: "Please enter your first name to create your account.",
    },
  },
  {
    code: EAuthErrorCodes.INVALID_NAME_SIGN_UP,
    label: "INVALID_NAME_SIGN_UP",
    expected: {
      title: "Invalid name",
      message: "Names can contain letters, numbers, spaces, apostrophes, periods, and hyphens (max 150 characters).",
    },
  },
];

describe("authErrorHandler — parity with the web copy for the migrated codes", () => {
  it.each(WEB_CONTRACT)("$label returns the web copy's exact title and message", ({ code, expected }) => {
    const info = authErrorHandler(code);
    expect(info, "code is unreachable — it is missing from bannerAlertErrorCodes").toBeDefined();
    expect(info?.title).toBe(expected.title);
    expect(info?.message).toBe(expected.message);
  });

  it("none of them fall through to the generic message", () => {
    for (const { code, label } of WEB_CONTRACT) {
      expect(authErrorHandler(code)?.message, `${label} fell through`).not.toBe(
        "Something went wrong. Please try again."
      );
    }
  });

  it("an unrecognised code is still undefined", () => {
    expect(authErrorHandler("9999" as EAuthErrorCodes)).toBeUndefined();
  });
});
