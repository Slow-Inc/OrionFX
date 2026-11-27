import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dashboard is served apart from the API (`dashboard_origin`); in development the
// API is proxied so the browser talks to one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
  },
});
