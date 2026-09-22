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
    // `"hidden"` rather than `true`: the maps are still generated, so an error
    // tracker can be given them, but the bundle carries no
    // `//# sourceMappingURL=` comment - so a browser never fetches one and the
    // source tree is not advertised to anyone who opens devtools. With `true`
    // every visitor's browser requests the map, which is the difference between
    // "the maps exist somewhere" and "the maps are published".
    //
    // This does not by itself keep them off a static host: `dist/` still
    // contains them. The deploy must exclude `*.map` (see the launch
    // checklist) - the comment removal is what makes them undiscoverable, and
    // the exclusion is what makes them unavailable.
    sourcemap: "hidden",
  },
});
