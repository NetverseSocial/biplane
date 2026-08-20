import { describe, expect, it } from "vitest";
import { isBlankName, normalizePersonName, validateOptionalPersonName, validatePersonName } from "./validation";

/**
 * THE SHARED BOUNDARY TABLE (Rowan RC 3085).
 *
 * One list, two sides. The identical table is asserted against the server
 * predicate in apps/api/plane/tests/unit/authentication/test_name_validation.py
 * — same characters, same expectations, both directions. It is not enough to
 * show the client accepts what the server accepts; it must also admit NOTHING
 * the server rejects, which is the direction that strands accounts.
 *
 * The shared boundary is Unicode category Nd: `\p{Nd}` here, `str.isdecimal()`
 * on the server. Every value below was probed on both runtimes, not reasoned
 * about.
 */
export const NUMERIC_BOUNDARY: Array<{ char: string; name: string; accepted: boolean }> = [
  { char: "7", name: "ASCII seven", accepted: true },
  { char: "٧", name: "Arabic-Indic seven", accepted: true },
  { char: "०", name: "Devanagari zero", accepted: true },
  { char: "²", name: "superscript two", accepted: false },
  { char: "⁵", name: "superscript five", accepted: false },
  { char: "½", name: "vulgar fraction one half", accepted: false },
  { char: "Ⅰ", name: "Roman numeral one", accepted: false },
];

describe("numeric boundary — the client must match the server in BOTH directions", () => {
  it.each(NUMERIC_BOUNDARY)("$name ($char) accepted=$accepted", ({ char, accepted }) => {
    const result = validatePersonName(`Nam${char}e`);
    if (accepted) expect(result).toBe(true);
    else expect(result).not.toBe(true);
  });

  it("admits nothing the server rejects", () => {
    const admittedButShouldNotBe = NUMERIC_BOUNDARY.filter(
      (c) => !c.accepted && validatePersonName(`Nam${c.char}e`) === true
    );
    expect(admittedButShouldNotBe.map((c) => c.name)).toEqual([]);
  });
});

/**
 * THE SHARED BLANKNESS TABLE (Rowan RC 3087).
 *
 * Mirrored in the Python suite. "Absent" used to mean `trim()` here and
 * `strip()` there, and those sets differ in BOTH directions — U+0085 and
 * U+001C..U+001F are blank to Python only, U+FEFF is blank to JavaScript only.
 * A last name of a single NEL was therefore absent to the server and a hard
 * error on the client. Both sides now use the explicit union.
 */
export const BLANKNESS_BOUNDARY: Array<{ cp: number; name: string; blank: boolean }> = [
  { cp: 0x20, name: "SPACE", blank: true },
  { cp: 0x09, name: "TAB", blank: true },
  { cp: 0x0a, name: "LF", blank: true },
  { cp: 0x85, name: "NEL — Python-only under library defaults", blank: true },
  { cp: 0x1c, name: "FS — Python-only under library defaults", blank: true },
  { cp: 0xfeff, name: "BOM — JavaScript-only under library defaults", blank: true },
  { cp: 0xa0, name: "NBSP", blank: true },
  { cp: 0x2028, name: "LINE SEPARATOR", blank: true },
  { cp: 0x41, name: "letter A — not blank", blank: false },
  { cp: 0x37, name: "digit 7 — not blank", blank: false },
];

describe("blankness — an optional field is absent on both sides or neither", () => {
  // Blankness is only observable on the REQUIRED path: a blank value is
  // "Name is required", while a non-blank one gets judged on its characters.
  // Asserting through the optional path instead would pass for any valid
  // letter, since "absent" and "present and fine" both return true.
  it.each(BLANKNESS_BOUNDARY)("$name is blank=$blank", ({ cp, blank }) => {
    const ch = String.fromCodePoint(cp);
    expect(isBlankName(ch)).toBe(blank);
    if (blank) {
      expect(validatePersonName(ch)).toBe("Name is required");
      expect(validateOptionalPersonName(ch)).toBe(true);
    } else {
      expect(validatePersonName(ch)).not.toBe("Name is required");
    }
  });

  it("a blank run of mixed separators is still absent", () => {
    const mixed = [0x20, 0x85, 0x1c, 0xfeff, 0x09].map((c) => String.fromCodePoint(c)).join("");
    expect(validateOptionalPersonName(mixed)).toBe(true);
  });
});

describe("validateOptionalPersonName — the optional-field rule itself", () => {
  // Exercising the rule the forms actually use, not just validatePersonName.
  // Written inline twice before, and both copies got this wrong: a
  // whitespace-only value is truthy, so it reached validatePersonName and came
  // back "Name is required" — re-imposing the requirement the wrapper removed.
  it.each([
    ["", "empty"],
    ["   ", "spaces only — the server trims this to absent"],
    ["\t", "tab only"],
  ])("treats %j as absent (%s)", (value) => {
    expect(validateOptionalPersonName(value)).toBe(true);
  });

  it.each([[undefined], [null]])("treats %s as absent", (value) => {
    expect(validateOptionalPersonName(value)).toBe(true);
  });

  it("still validates a value that was actually supplied", () => {
    expect(validateOptionalPersonName("Wright")).toBe(true);
    expect(validateOptionalPersonName("7of9")).toBe(true);
    expect(validateOptionalPersonName("a<script>")).not.toBe(true);
    expect(validateOptionalPersonName("Half½")).not.toBe(true);
  });
});

/**
 * BIP-21, frontend half.
 *
 * Fixing the server predicate alone was not enough and nearly shipped that way.
 * The server would accept `7of9` and create the account, and then this
 * validator — which both onboarding forms call — would refuse the same
 * prefilled name, stranding a brand-new user in required onboarding with no way
 * forward. That is worse than the original bug: the account exists but cannot be
 * used. John reproduced it by hand while this PR was in review.
 *
 * So these cases are pinned against the PRODUCTION validator, and the policy
 * they encode is the server's: apps/api/plane/authentication/views/app/email.py
 * `name_error_code`. If the two drift again, the failure is silent and lands on
 * a real person, so the digit and period cases below are load-bearing, not
 * decorative.
 */

describe("validatePersonName — must accept everything the server accepts", () => {
  it.each([
    ["7of9", "the account that could not be created, then could not onboard"],
    ["R2", "digits at the end"],
    ["Seven9", "digit inside a word"],
    ["3", "a name that is only a digit"],
    ["J. R. Smith", "periods — server-legal, and this validator used to refuse them"],
    ["Ali ٧", "non-ASCII digit"],
  ])("accepts %j (%s)", (name) => {
    expect(validatePersonName(name)).toBe(true);
  });

  it.each([["Sable"], ["Mary-Jane"], ["O'Brien"], ["Renée"], ["李雷"], ["محمد"]])("still accepts %j", (name) => {
    expect(validatePersonName(name)).toBe(true);
  });
});

describe("validatePersonName — what it must still refuse", () => {
  it.each([
    ["John<script>", "markup"],
    ["Bad\tName", "tab"],
    ["Bad\nName", "newline"],
    ["BadName", "NEL — isspace() is true for it, which is why an allowlist is used"],
    ["Bad Name", "line separator"],
    ["a@b", "at sign"],
  ])("rejects %j (%s)", (name) => {
    expect(validatePersonName(name)).toBe(
      "Names can only contain letters, numbers, spaces, periods, hyphens, and apostrophes"
    );
  });

  it("still requires a name", () => {
    expect(validatePersonName("")).toBe("Name is required");
    expect(validatePersonName("   ")).toBe("Name is required");
  });

  it("still enforces its own length cap", () => {
    // Tighter than the server's 150. Safe direction: it can only reject early,
    // never admit something the server will refuse.
    expect(validatePersonName("a".repeat(51))).toBe("Name must be 50 characters or less");
    expect(validatePersonName("a".repeat(50))).toBe(true);
  });
});

/**
 * THE MIXED TABLE — blank characters ADJACENT to a valid name.
 * (Morrow RC 3092 / Rowan RC 3091.)
 *
 * Mirrored in the Python suite as MIXED_BOUNDARY.
 *
 * The case both earlier tables missed. They covered "which characters are
 * digits" and "which characters mean absent", but not "a blank character
 * sitting next to a real name". The server stripped before validating while
 * this side validated the raw string, so "<NEL>Alice" was server-accepted and
 * client-rejected — and the raw value was what got stored, so a leading control
 * character reached the database having never been checked.
 *
 * The policy: normalizePersonName is the canonical form, that is what gets
 * validated, and that is what gets stored.
 */
export const MIXED_BOUNDARY: Array<{ cps: number[]; name: string; label: string }> = [
  { cps: [0x85], name: "Alice", label: "leading NEL" },
  { cps: [0xfeff], name: "Alice", label: "leading BOM" },
  { cps: [0x1c], name: "Alice", label: "leading FS" },
  { cps: [0x20], name: "Alice", label: "leading space" },
  { cps: [0x85, 0xfeff], name: "Alice", label: "leading NEL + BOM" },
];

const fromCps = (cps: number[]) => cps.map((c) => String.fromCodePoint(c)).join("");

describe("mixed — blank characters adjacent to a valid name", () => {
  it.each(MIXED_BOUNDARY)("$label is accepted and normalised away", ({ cps, name }) => {
    const raw = fromCps(cps) + name;
    expect(validatePersonName(raw)).toBe(true);
    expect(normalizePersonName(raw)).toBe(name);
  });

  it.each(MIXED_BOUNDARY)("trailing $label too", ({ cps, name }) => {
    const raw = name + fromCps(cps);
    expect(validatePersonName(raw)).toBe(true);
    expect(normalizePersonName(raw)).toBe(name);
  });

  it("a blank INSIDE a name is still rejected", () => {
    // Only the ends are stripped. A separator in the middle is what RC 3029
    // exists to refuse, and must stay refused.
    for (const cp of [0x85, 0xfeff, 0x1c, 0x2028]) {
      const raw = "Al" + String.fromCodePoint(cp) + "ice";
      expect(validatePersonName(raw), `U+${cp.toString(16)}`).not.toBe(true);
    }
  });

  it("what is validated is what is stored", () => {
    const raw = String.fromCodePoint(0xfeff) + "Alice" + String.fromCodePoint(0x85);
    const canonical = normalizePersonName(raw);
    expect(canonical).toBe("Alice");
    expect(validatePersonName(canonical)).toBe(true);
    expect(canonical).not.toBe(raw);
  });

  it("an optional field of blanks around nothing is still absent", () => {
    expect(validateOptionalPersonName(fromCps([0x85, 0xfeff, 0x20]))).toBe(true);
  });
});
