/** Turn an axios import-url error into a React-safe string. */
export function formatImportErrorDetail(error) {
  const detail = error?.response?.data?.detail;
  if (detail == null) return "Import failed.";
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}
