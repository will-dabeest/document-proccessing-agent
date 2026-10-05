/** Ask results show the first 200 characters of each snippet, then an ellipsis. */
export function formatAskSnippet(text) {
  return `${text?.slice(0, 200) ?? ""}…`;
}
