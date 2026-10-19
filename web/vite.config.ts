/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API (FastAPI, `uv run ...`) listens on :8000. In dev the browser only talks to
// Vite, which forwards `/api` there, so the page needs no CORS and the SSE stream keeps
// one origin. `vite preview` uses the same proxy.
const api = {
  "/api": {
    target: "http://localhost:8000",
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy: api },
  preview: { port: 4173, proxy: api },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
