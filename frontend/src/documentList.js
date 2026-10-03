/** Normalize GET /documents JSON into the list the UI renders. */
export function documentsFromResponse(data) {
  return data.items || [];
}

/** Stable React key: Dynamo MessageId, or a positional fallback. */
export function documentRowKey(row, index) {
  return row.message_id || `row-${index}`;
}

/** Only the exact Completed status uses the done badge. */
export function isCompletedStatus(status) {
  return status === "Completed";
}

/** Missing status is shown as Unknown rather than a blank badge. */
export function statusBadgeLabel(status) {
  return status || "Unknown";
}
