import assert from "node:assert/strict";
import test from "node:test";

import { formatImportErrorDetail } from "../src/importError.js";

test("missing axios response falls back to a generic import failure", () => {
  assert.equal(formatImportErrorDetail(undefined), "Import failed.");
  assert.equal(formatImportErrorDetail(null), "Import failed.");
  assert.equal(
    formatImportErrorDetail(new Error("Network Error")),
    "Import failed.",
  );
  assert.equal(
    formatImportErrorDetail({ response: { data: {} } }),
    "Import failed.",
  );
  assert.equal(
    formatImportErrorDetail({ response: { data: { detail: null } } }),
    "Import failed.",
  );
});

test("string import failures are shown verbatim, including blank detail", () => {
  assert.equal(
    formatImportErrorDetail({
      response: { data: { detail: "URL host is not allowed" } },
    }),
    "URL host is not allowed",
  );
  assert.equal(
    formatImportErrorDetail({ response: { data: { detail: "" } } }),
    "",
  );
});

test("FastAPI validation detail is stringified so React can render it", () => {
  const detail = [
    {
      type: "missing",
      loc: ["body", "url"],
      msg: "Field required",
    },
  ];
  const formatted = formatImportErrorDetail({
    response: { data: { detail } },
  });

  assert.equal(typeof formatted, "string");
  assert.equal(formatted, JSON.stringify(detail));
  assert.equal(
    formatImportErrorDetail({
      response: { data: { detail: { msg: "blocked" } } },
    }),
    JSON.stringify({ msg: "blocked" }),
  );
});
