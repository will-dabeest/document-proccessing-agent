import assert from "node:assert/strict";
import { File } from "node:buffer";
import test from "node:test";

import {
  DOCUMENTS_QUERY_KEY,
  DOCUMENTS_REFETCH_INTERVAL_MS,
  buildImportUrlBody,
  buildUploadForm,
  canSubmitImportUrl,
  canSubmitUpload,
  documentsInvalidationFilter,
  documentsListQuery,
} from "../src/clientContracts.js";

test("document list polls the same key that uploads and imports invalidate", () => {
  const queryFn = () => [];
  const query = documentsListQuery(queryFn);
  const invalidation = documentsInvalidationFilter();

  assert.equal(query.queryKey, DOCUMENTS_QUERY_KEY);
  assert.equal(invalidation.queryKey, DOCUMENTS_QUERY_KEY);
  assert.deepEqual(query.queryKey, ["documents"]);
  assert.equal(query.refetchInterval, DOCUMENTS_REFETCH_INTERVAL_MS);
  assert.equal(query.refetchInterval, 3000);
  assert.equal(query.queryFn, queryFn);
});

test("upload form uses the file part the ingestion API expects", () => {
  const notes = new File(["hello"], "notes.txt", { type: "text/plain" });
  const form = buildUploadForm(notes);

  assert.deepEqual([...form.keys()], ["file"]);
  const part = form.get("file");
  assert.equal(part.name, "notes.txt");
  assert.equal(part, notes);
  assert.equal(canSubmitUpload(notes), true);
  assert.equal(canSubmitUpload(null), false);
  assert.equal(canSubmitUpload(undefined), false);
});

test("import URL is trimmed and a whitespace-only value cannot be submitted", () => {
  assert.deepEqual(buildImportUrlBody("  https://example.com/a  "), {
    url: "https://example.com/a",
  });
  assert.deepEqual(buildImportUrlBody("\nhttps://example.com/path?q=1\t"), {
    url: "https://example.com/path?q=1",
  });
  assert.deepEqual(buildImportUrlBody("https://example.com/a b"), {
    url: "https://example.com/a b",
  });

  assert.equal(canSubmitImportUrl("  https://example.com/a  "), true);
  assert.equal(canSubmitImportUrl("   "), false);
  assert.equal(canSubmitImportUrl("\n\t"), false);
  assert.equal(canSubmitImportUrl(""), false);
  assert.deepEqual(buildImportUrlBody("   "), { url: "" });
});
