import assert from "node:assert/strict";
import test from "node:test";

import { formatAskSnippet } from "../src/askSnippet.js";

const KEPT = "字";
const DROPPED = "尾";

test("formatAskSnippet keeps 200 characters and always adds an ellipsis", () => {
  const short = "a".repeat(10);
  assert.equal(formatAskSnippet(short), `${short}…`);

  const exact = "b".repeat(199) + KEPT;
  assert.equal(exact.length, 200);
  assert.equal(formatAskSnippet(exact), `${exact}…`);
});

test("formatAskSnippet drops the 201st character", () => {
  const text = "c".repeat(199) + KEPT + DROPPED + "d".repeat(30);
  const formatted = formatAskSnippet(text);

  assert.equal(formatted, `${"c".repeat(199)}${KEPT}…`);
  assert.equal(formatted.includes(DROPPED), false);
  assert.equal(formatted.includes("d"), false);
});

test("formatAskSnippet shows only an ellipsis when snippet text is missing", () => {
  assert.equal(formatAskSnippet(undefined), "…");
  assert.equal(formatAskSnippet(null), "…");
  assert.equal(formatAskSnippet(""), "…");
});
