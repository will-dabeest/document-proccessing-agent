import assert from "node:assert/strict";
import test from "node:test";

import { buildAskBody } from "../src/askRequest.js";

test("ask body keeps the question field exactly as typed", () => {
  assert.deepEqual(buildAskBody("  what is the policy?  "), {
    question: "  what is the policy?  ",
  });
  assert.deepEqual(buildAskBody("cost of\nitem A"), {
    question: "cost of\nitem A",
  });
  assert.deepEqual(buildAskBody("alpha  beta"), { question: "alpha  beta" });
  assert.deepEqual(buildAskBody(""), { question: "" });
  assert.deepEqual(buildAskBody("   "), { question: "   " });
});
