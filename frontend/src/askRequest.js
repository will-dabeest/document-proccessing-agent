/** POST /ask body. Ingestion requires the `question` field. The client forwards it unchanged. */

export function buildAskBody(question) {
  return { question };
}
