import onlyCoauthorTrailers from "./tools/commitlint/only-coauthor-trailers.mjs";

// Commit messages are exactly one line: `type(optional-scope): subject`, at
// most 100 characters. The only thing allowed after the header is a blank
// line followed by `Co-authored-by: Name <email>` trailers. Anything longer
// belongs in the pull request description.
export default {
  extends: ["@commitlint/config-conventional"],
  plugins: [{ rules: { "only-coauthor-trailers": onlyCoauthorTrailers } }],
  rules: {
    "header-max-length": [2, "always", 100],
    "type-enum": [
      2,
      "always",
      [
        "feat",
        "fix",
        "docs",
        "chore",
        "refactor",
        "ci",
        "build",
        "perf",
        "style",
        "test",
        "revert",
      ],
    ],
    "only-coauthor-trailers": [2, "always"],
    // The local rule above is the authority on everything after the header;
    // the built-in body/footer rules would only produce confusing duplicates.
    "body-max-line-length": [0],
    "footer-max-line-length": [0],
    "body-leading-blank": [0],
    "footer-leading-blank": [0],
  },
};
