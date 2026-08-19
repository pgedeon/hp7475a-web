import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: REST and WS both go through Vite to the backend on :8750.
// If the WS proxy misbehaves, set VITE_WS_URL=ws://127.0.0.1:8750/api/ws/status
// and the client connects directly (see src/api/ws.ts + README).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8750",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    // forks pool hangs on this WSL2 host; threads pool is reliable here
    pool: "threads",
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      include: ["src/**"],
      exclude: ["src/test/**", "src/main.tsx"],
      thresholds: { lines: 80, functions: 80 },
    },
  },
});
