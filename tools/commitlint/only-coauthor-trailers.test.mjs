import assert from "node:assert/strict";
import { test } from "node:test";

import onlyCoauthorTrailers from "./only-coauthor-trailers.mjs";

const check = (raw) => onlyCoauthorTrailers({ raw })[0];

test("accepts a single-line message", () => {
  assert.equal(check("feat: add state bucket"), true);
});

test("accepts a header followed by Co-authored-by trailers", () => {
  assert.equal(
    check(
      "fix(ci): pin tflint\n\nCo-authored-by: Someone <someone@example.com>\nCo-authored-by: Other <other@example.com>",
    ),
    true,
  );
});

test("ignores blank and comment lines", () => {
  assert.equal(check("chore: tidy\n\n# Please enter the commit message\n#\n"), true);
});

test("rejects a free-form body", () => {
  assert.equal(check("feat: add zone\n\nThis adds the hosted zone."), false);
});

test("rejects trailers other than Co-authored-by", () => {
  assert.equal(check("feat: add zone\n\nSigned-off-by: Someone <someone@example.com>"), false);
});

test("rejects a malformed Co-authored-by trailer", () => {
  assert.equal(check("feat: add zone\n\nCo-authored-by: no email here"), false);
});

test("supports the never flavour", () => {
  assert.equal(onlyCoauthorTrailers({ raw: "feat: x\n\nbody" }, "never")[0], true);
});
