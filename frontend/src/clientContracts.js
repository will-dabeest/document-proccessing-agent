/** Shared request and query contracts between the UI and the ingestion API. */

export const DOCUMENTS_QUERY_KEY = ["documents"];
export const DOCUMENTS_REFETCH_INTERVAL_MS = 3000;

export function documentsListQuery(queryFn) {
  return {
    queryKey: DOCUMENTS_QUERY_KEY,
    queryFn,
    refetchInterval: DOCUMENTS_REFETCH_INTERVAL_MS,
  };
}

export function documentsInvalidationFilter() {
  return { queryKey: DOCUMENTS_QUERY_KEY };
}

/** FastAPI `UploadFile = File(...)` reads the multipart part named `file`. */
export function buildUploadForm(file) {
  const form = new FormData();
  form.append("file", file);
  return form;
}

export function canSubmitUpload(file) {
  return Boolean(file);
}

/** Import rejects surrounding whitespace; a blank URL must not be posted. */
export function buildImportUrlBody(url) {
  return { url: url.trim() };
}

export function canSubmitImportUrl(url) {
  return Boolean(url.trim());
}
