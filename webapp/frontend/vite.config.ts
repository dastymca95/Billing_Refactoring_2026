import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During dev the React app runs on :5173 and proxies /api/* to the
// FastAPI backend. Two ways to point it at a different backend:
//   * VITE_API_BASE_URL=http://backend:8000   (Docker compose case —
//     containers see each other by service name)
//   * VITE_BACKEND_PORT=8001                  (local dev case — same host)
// `VITE_API_BASE_URL` wins when both are set.
const explicitBase = process.env.VITE_API_BASE_URL?.trim();
const backendPort = process.env.VITE_BACKEND_PORT ?? "8000";
const proxyTarget = explicitBase || `http://localhost:${backendPort}`;

export default defineConfig({
  plugins: [react()],
  // pdfjs-dist v4 emits top-level `await`; ship a target that supports
  // it. Modern browsers (Chrome 89+, Edge 89+, Firefox 89+, Safari 15+)
  // handle this fine.
  build: {
    target: "es2022",
  },
  esbuild: {
    target: "es2022",
  },
  optimizeDeps: {
    esbuildOptions: { target: "es2022" },
  },
  server: {
    host: process.env.HOST ?? "localhost",
    port: 5173,
    proxy: {
      "/api": {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
});
