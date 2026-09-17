import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The admin UI talks to the FastAPI control plane. In dev we proxy `/api` to
// the local API (default port 8000) so the SPA can be served by Vite alone.
// Override with VITE_API_BASE_URL when talking to a remote/containerized API.
const API_TARGET = process.env.VITE_API_TARGET || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
