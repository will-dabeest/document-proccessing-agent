import assert from "node:assert/strict";
import test from "node:test";

import {
  documentRowKey,
  documentsFromResponse,
  isCompletedStatus,
  statusBadgeLabel,
} from "../src/documentList.js";

test("documentsFromResponse uses items and treats a missing list as empty", () => {
  const items = [{ message_id: "m1", status: "Completed" }];
  assert.equal(documentsFromResponse({ items }), items);
  assert.deepEqual(documentsFromResponse({}), []);
  assert.deepEqual(documentsFromResponse({ items: null }), []);
  assert.deepEqual(documentsFromResponse({ items: undefined }), []);
});

test("documentRowKey prefers message_id and falls back when it is blank", () => {
  assert.equal(documentRowKey({ message_id: "job-1" }, 0), "job-1");
  assert.equal(documentRowKey({}, 2), "row-2");
  assert.equal(documentRowKey({ message_id: "" }, 4), "row-4");
});

test("status badge keeps Completed distinct and labels a missing status Unknown", () => {
  assert.equal(isCompletedStatus("Completed"), true);
  assert.equal(statusBadgeLabel("Completed"), "Completed");

  for (const status of ["Processing", "Failed", "completed", "Unknown"]) {
    assert.equal(isCompletedStatus(status), false);
    assert.equal(statusBadgeLabel(status), status);
  }

  for (const status of [undefined, null, ""]) {
    assert.equal(isCompletedStatus(status), false);
    assert.equal(statusBadgeLabel(status), "Unknown");
  }
});
