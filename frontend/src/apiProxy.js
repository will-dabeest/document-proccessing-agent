/** Strip the dev-server `/api` prefix so Vite forwards to the ingestion app root. */
export function rewriteApiPath(path) {
  return path.replace(/^\/api/, "");
}

export function ingestionProxyTarget(env = process.env) {
  return env.INGESTION_PROXY_TARGET || "http://127.0.0.1:8000";
}

export function apiProxyOptions(env = process.env) {
  return {
    target: ingestionProxyTarget(env),
    changeOrigin: true,
    rewrite: rewriteApiPath,
  };
}
