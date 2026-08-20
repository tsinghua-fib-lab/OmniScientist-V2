import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:1088",
      "/health": "http://127.0.0.1:1088",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Async Settings / markdown chunks must not be modulepreloaded from
    // index.html; otherwise the browser still downloads the old 500 kB graph
    // on first paint. Loopback serving fetches them when the UI needs them.
    modulePreload: false,
  },
});
