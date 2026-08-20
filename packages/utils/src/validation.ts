/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * Input Validation Utilities
 * Following OWASP Input Validation best practices using allowlist approach
 *
 * Security: Blocks injection-risk characters: < > ' " % # { } [ ] * ^ !
 * These patterns are designed to prevent XSS, SQL injection, template injection,
 * and other security vulnerabilities while maintaining good UX
 */

// =============================================================================
// VALIDATION REGEX PATTERNS
// =============================================================================

/**
 * Person Name Pattern (for first_name, last_name)
 * Allows: Unicode letters (\p{L}), digits (\p{N}), spaces, periods, hyphens, apostrophes
 * Use case: Accommodates international names like "José", "李明", "محمد", "Müller"
 * Blocks: Injection-risk characters and special symbols
 *
 * biplane (BIP-21): this MUST stay in step with the server predicate
 * `name_error_code` in apps/api/plane/authentication/views/app/email.py.
 * When it did not, the two disagreed in the dangerous direction: the server
 * accepted a name the client then refused, so a fresh account could be created
 * and immediately stranded in required onboarding, which is exactly what
 * happened to `7of9`. Digits and the period are both server-legal, so both
 * belong here.
 *
 * `\p{Nd}`, NOT `\p{N}`. `\p{N}` is every Unicode number — it admits ½ and Ⅰ,
 * which the server refuses, so it recreated the same client/server split in the
 * opposite direction (Rowan RC 3085). `\p{Nd}` is exactly Python's
 * `str.isdecimal()`, which is what the server now uses, so both sides are
 * written to one boundary rather than two that happen to overlap:
 *
 *   ASCII 7, Arabic-Indic ٧, Devanagari ० -> accepted by both
 *   superscript ², vulgar ½, Roman Ⅰ      -> rejected by both
 *
 * The 50-character cap below is deliberately tighter than the server's 150.
 * That divergence is safe in the way this one was not — it can only reject
 * early, never admit something the server will later refuse.
 */
// Curly quotes included for the same macOS smart-quote substitution as company names.
//
// A literal space, NOT `\s`. JavaScript's `\s` matches tab, newline, and the
// Unicode line/paragraph separators, all of which the server refuses — so with
// `\s` this validator passed names the server would then reject, which is the
// same class of client/server disagreement as the digit case, just pointing the
// other way. The server comment (RC 3029) says "plain space ONLY" for exactly
// this reason; now both say it.
export const PERSON_NAME_REGEX = /^[\p{L}\p{Nd} '’‘.-]+$/u;

/**
 * Display Name Pattern (for display_name, usernames)
 * Allows: Unicode letters (\p{L}), numbers (\p{N}), underscore, period, hyphen
 * Use case: International usernames like "josé_123", "李明.dev", "müller-2024"
 * Blocks: Spaces and injection-risk characters
 */
export const DISPLAY_NAME_REGEX = /^[\p{L}\p{N}_.-]+$/u;

/**
 * Company/Organization Name Pattern (for company_name, workspace names)
 * Allows: Unicode letters (\p{L}), numbers (\p{N}), spaces, underscores, hyphens
 * Use case: International business names like "Société Générale", "株式会社", "Müller GmbH"
 * Blocks: Special punctuation and injection-risk chars
 */
// biplane: legal company names carry punctuation — "Netverse Social, Inc." must type
// cleanly, and so must "O’Brien & Sons": macOS smart-quotes substitutes the TYPOGRAPHIC
// apostrophe (’ U+2019), so the straight ' alone is not enough.
export const COMPANY_NAME_REGEX = /^[\p{L}\p{N}\s_\-.,&'’‘()+]+$/u;

/**
 * URL Slug Pattern (for workspace slugs, URL-safe identifiers)
 * Allows: Unicode letters (\p{L}), numbers (\p{N}), underscores, hyphens
 * Use case: International URL-safe identifiers like "josé-workspace", "李明-project"
 * Blocks: Spaces and special characters (URL encoding will handle Unicode in actual URLs)
 */
export const SLUG_REGEX = /^[\p{L}\p{N}_-]+$/u;

// =============================================================================
// VALIDATION FUNCTIONS
// =============================================================================

/**
 * biplane (BIP-21, Rowan RC 3087): the ONE blankness policy, shared with the
 * server's `NAME_BLANK_CHARS` in
 * apps/api/plane/authentication/views/app/email.py.
 *
 * "Is this optional field absent?" was previously `String.prototype.trim()`
 * here and `str.strip()` there, and those two sets are NOT the same — they
 * disagree in both directions:
 *
 *   U+0085 NEL, U+001C..U+001F  blank to Python, NOT blank to JavaScript
 *   U+FEFF BOM                  blank to JavaScript, NOT blank to Python
 *
 * So a last name of a single NEL was absent to the server and a hard error on
 * the client, which is the account-stranding direction all over again; and a
 * lone BOM was the reverse. Neither library default is wrong, they are just
 * different, which is exactly why this cannot be left to a library default.
 *
 * This set is the UNION of both, so each side treats as absent everything the
 * other would. Adding a character here means adding it there.
 */
const NAME_BLANK_CHARS =
  /[\t\n\v\f\r\u001c-\u001f\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]/gu;

/**
 * @description True when an optional name field should be treated as not supplied.
 * @param {string | undefined | null} name - The raw field value
 */
export const isBlankName = (name: string | undefined | null): boolean => {
  if (!name) return true;
  return name.replace(NAME_BLANK_CHARS, "") === "";
};

/**
 * biplane (BIP-21, Morrow exact-head find on 54b88f0): strip the shared blank
 * characters from the ENDS, and validate THAT.
 *
 * The bug this closes: the server stripped before validating while this side
 * used blankness only to choose absent-versus-validate and then validated the
 * ORIGINAL string. So `"Alice"` was accepted by the server, which saw
 * "Alice", and rejected here, which saw a leading NEL — the account-stranding
 * direction for the fourth time in this PR.
 *
 * Edge-only, deliberately. `isBlankName` above strips globally because it only
 * asks "is there anything but blanks in here"; normalisation must not eat the
 * space in "Mary Jane".
 */
const NAME_EDGE_BLANKS = new RegExp(`^(?:${NAME_BLANK_CHARS.source})+|(?:${NAME_BLANK_CHARS.source})+$`, "gu");

/**
 * @description Canonical form of a name field: the value with shared blank
 * characters removed from both ends. Validate and store THIS, never the raw
 * input, or the two disagree and unvalidated characters reach the database.
 * @param {string | undefined | null} name - The raw field value
 */
export const normalizePersonName = (name: string | undefined | null): string =>
  (name ?? "").replace(NAME_EDGE_BLANKS, "");

/**
 * @description Validates person names (first name, last name)
 * @param {string} name - Name to validate
 * @returns {boolean | string} true if valid, error message if invalid
 * @example
 * validatePersonName("John") // returns true
 * validatePersonName("O'Brien") // returns true
 * validatePersonName("Jean-Paul") // returns true
 * validatePersonName("John<script>") // returns error message
 */
export const validatePersonName = (name: string): boolean | string => {
  // Validate the CANONICAL form, exactly as the server does. Validating the raw
  // string while the server validated its stripped form is what made
  // "<NEL>Alice" server-accepted and client-rejected.
  const value = normalizePersonName(name);

  if (value === "") {
    return "Name is required";
  }

  if (value.length > 50) {
    return "Name must be 50 characters or less";
  }

  // biplane: no hasInjectionRiskChars gate — it bans the apostrophe this
  // validator's own docstring promises to accept ("O'Brien"). The allowlist
  // regex already excludes every injection-risk character except ' ’ ‘.
  if (!PERSON_NAME_REGEX.test(value)) {
    return "Names can only contain letters, numbers, spaces, periods, hyphens, and apostrophes";
  }

  return true;
};

/**
 * @description Validates an OPTIONAL person name — a last name, typically.
 * Blank, or blank after trimming, means "not supplied" and is accepted.
 * @param {string | undefined | null} name - Name to validate, if one was given
 * @returns {boolean | string} true if valid or absent, error message if invalid
 * @example
 * validateOptionalPersonName("")      // true — absent
 * validateOptionalPersonName("   ")   // true — absent after trim
 * validateOptionalPersonName("Wright") // true
 * validateOptionalPersonName("a<b")   // error message
 *
 * biplane (BIP-21): this exists as a real exported function, not as an inline
 * `!value || validatePersonName(value)` in each form, for two reasons. It was
 * written inline twice and both copies were wrong in the same way — a
 * whitespace-only value is truthy, so it reached validatePersonName and came
 * back "Name is required", re-imposing the requirement the wrapper existed to
 * remove (Rowan RC 3085). And an inline lambda inside a .tsx form is not
 * reachable by any test, so neither copy could be pinned. The trim matches the
 * server, which does `str(value or "").strip()` and treats blank as absent.
 */
export const validateOptionalPersonName = (name: string | undefined | null): boolean | string => {
  if (normalizePersonName(name) === "") return true;
  return validatePersonName(name as string);
};

/**
 * @description Validates display names and usernames
 * @param {string} displayName - Display name to validate
 * @returns {boolean | string} true if valid, error message if invalid
 * @example
 * validateDisplayName("john_doe") // returns true
 * validateDisplayName("john.doe-123") // returns true
 * validateDisplayName("john doe") // returns error message (spaces not allowed)
 * validateDisplayName("john<>doe") // returns error message
 */
export const validateDisplayName = (displayName: string): boolean | string => {
  if (!displayName || displayName.trim() === "") {
    return true; // Display name is optional in most cases
  }

  if (displayName.length > 50) {
    return "Display name must be 50 characters or less";
  }

  if (hasInjectionRiskChars(displayName)) {
    return "Display name cannot contain special characters like < > ' \" { } [ ] * ^ ! # %";
  }

  if (!DISPLAY_NAME_REGEX.test(displayName)) {
    return "Display name can only contain letters, numbers, periods, hyphens, and underscores";
  }

  return true;
};

/**
 * @description Validates company and organization names
 * @param {string} companyName - Company name to validate
 * @param {boolean} required - Whether the field is required
 * @returns {boolean | string} true if valid, error message if invalid
 * @example
 * validateCompanyName("Acme Corp") // returns true
 * validateCompanyName("Acme_Corp-123") // returns true
 * validateCompanyName("Acme{Corp}") // returns error message
 */
export const validateCompanyName = (companyName: string, required: boolean = false): boolean | string => {
  if (!companyName || companyName.trim() === "") {
    return required ? "Company name is required" : true;
  }

  if (companyName.length > 80) {
    return "Company name must be 80 characters or less";
  }

  // biplane: no hasInjectionRiskChars gate here — its denylist bans the straight
  // apostrophe that COMPANY_NAME_REGEX deliberately allows (it rejected "O'Brien"
  // before the regex was ever consulted). The regex is a strict allowlist and
  // already excludes every injection-risk character except the quotes we permit.
  if (!COMPANY_NAME_REGEX.test(companyName)) {
    return "Company name can only contain letters, numbers, spaces, and common punctuation (. , & ' - _ + parentheses)";
  }

  return true;
};

/**
 * @description Validates company and organization names
 * @param {string} workspaceName - Workspace name to validate
 * @param {boolean} required - Whether the field is required
 * @returns {boolean | string} true if valid, error message if invalid
 * @example
 * validateWorkspaceName("Acme Corp") // returns true
 * validateWorkspaceName("Acme_Corp-123") // returns true
 * validateWorkspaceName("Acme{Corp}") // returns error message
 */
export const validateWorkspaceName = (workspaceName: string, required: boolean = false): boolean | string => {
  if (!workspaceName || workspaceName.trim() === "") {
    return required ? "Workspace name is required" : true;
  }

  if (workspaceName.length > 80) {
    return "Workspace name must be 80 characters or less";
  }

  if (hasInjectionRiskChars(workspaceName)) {
    return "Workspace name cannot contain special characters like < > ' \" { } [ ] * ^ ! # %";
  }

  if (!COMPANY_NAME_REGEX.test(workspaceName)) {
    return "Workspace name can only contain letters, numbers, spaces, hyphens, and underscores";
  }

  return true;
};

/**
 * @description Validates URL slugs and identifiers
 * @param {string} slug - Slug to validate
 * @returns {boolean | string} true if valid, error message if invalid
 * @example
 * validateSlug("my-workspace") // returns true
 * validateSlug("my_workspace_123") // returns true
 * validateSlug("my workspace") // returns error message (spaces not allowed)
 */
export const validateSlug = (slug: string): boolean | string => {
  if (!slug || slug.trim() === "") {
    return "Slug is required";
  }

  if (slug.length > 48) {
    return "Slug must be 48 characters or less";
  }

  if (hasInjectionRiskChars(slug)) {
    return "Slug cannot contain special characters like < > ' \" { } [ ] * ^ ! # %";
  }

  if (!SLUG_REGEX.test(slug)) {
    return "Slug can only contain letters, numbers, hyphens, and underscores";
  }

  return true;
};

/**
 * @description Checks if a string contains any injection-risk characters
 * @param {string} input - String to check
 * @returns {boolean} true if injection-risk characters found
 * @example
 * hasInjectionRiskChars("Hello World") // returns false
 * hasInjectionRiskChars("Hello<script>") // returns true
 */
export const hasInjectionRiskChars = (input: string): boolean => {
  const injectionRiskPattern = /[<>'"{}[\]*^!#%]/;
  return injectionRiskPattern.test(input);
};
