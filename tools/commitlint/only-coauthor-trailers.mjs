/**
 * commitlint rule `only-coauthor-trailers`.
 *
 * A commit message is a single header line. The only content allowed after
 * it is a blank line or a `Co-authored-by: Name <email>` trailer. Comment
 * lines (`#`) are ignored because `git commit` editors leave them in the
 * message file; git strips them before the commit is created.
 *
 * This rule inspects the raw message rather than commitlint's parsed
 * body/footer so that it cannot be sidestepped by trailer-like formatting.
 */
export const COAUTHOR_TRAILER = /^Co-authored-by: .+ <.+>$/;

/**
 * @param {{ raw?: string | null }} parsed
 * @param {'always' | 'never'} [when]
 * @returns {[boolean, string]}
 */
export default function onlyCoauthorTrailers(parsed, when = "always") {
  const raw = parsed.raw ?? "";
  const [, ...rest] = raw.split(/\r?\n/);

  const offending = rest
    .map((line) => line.trimEnd())
    .filter((line) => line.trim() !== "" && !line.startsWith("#"))
    .filter((line) => !COAUTHOR_TRAILER.test(line));

  const passes = offending.length === 0;
  const negated = when === "never";

  return [
    negated ? !passes : passes,
    negated
      ? "commit message must contain something other than Co-authored-by trailers"
      : `commit message must be a single line; only "Co-authored-by: Name <email>" trailers may follow it. Offending line(s): ${offending
          .map((line) => JSON.stringify(line))
          .join(", ")}`,
  ];
}
