import assert from "node:assert/strict";
import test from "node:test";

import {
  apiProxyOptions,
  ingestionProxyTarget,
  rewriteApiPath,
} from "../src/apiProxy.js";

test("rewriteApiPath strips only the leading /api prefix", () => {
  assert.equal(rewriteApiPath("/api/documents"), "/documents");
  assert.equal(rewriteApiPath("/api/upload"), "/upload");
  assert.equal(rewriteApiPath("/api/import-url"), "/import-url");
  assert.equal(rewriteApiPath("/api/ask"), "/ask");
  assert.equal(rewriteApiPath("/api/internal/index"), "/internal/index");
  assert.equal(rewriteApiPath("/api"), "");
  assert.equal(rewriteApiPath("/api/"), "/");
  assert.equal(
    rewriteApiPath("/api/documents?status=Completed"),
    "/documents?status=Completed",
  );
  assert.equal(rewriteApiPath("/api/api/documents"), "/api/documents");
  assert.equal(rewriteApiPath("/health"), "/health");
});

test("ingestion proxy defaults to local ingestion and honors an explicit target", () => {
  assert.equal(ingestionProxyTarget({}), "http://127.0.0.1:8000");
  assert.equal(
    ingestionProxyTarget({ INGESTION_PROXY_TARGET: "" }),
    "http://127.0.0.1:8000",
  );
  assert.equal(
    ingestionProxyTarget({ INGESTION_PROXY_TARGET: "http://ingestion:8000" }),
    "http://ingestion:8000",
  );
});

test("apiProxyOptions wires rewrite, changeOrigin, and target", () => {
  const options = apiProxyOptions({
    INGESTION_PROXY_TARGET: "http://ingestion:8000",
  });

  assert.equal(options.target, "http://ingestion:8000");
  assert.equal(options.changeOrigin, true);
  assert.equal(options.rewrite, rewriteApiPath);
  assert.equal(options.rewrite("/api/ask"), "/ask");
});
