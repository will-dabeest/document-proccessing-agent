import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

import { apiProxyOptions } from "./src/apiProxy.js";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": apiProxyOptions(),
    },
  },
});
