import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The console's Python server (src/ouroboros/gui/server.py) serves this
// directory as-is, so the build writes straight into the package.
const target = process.env.OUROBOROS_GUI_TARGET ?? "http://127.0.0.1:10300";

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../src/ouroboros/gui/static",
    emptyOutDir: true,
    target: "es2022",
    cssCodeSplit: false,
    sourcemap: false,
    assetsInlineLimit: 0,
    // One chunk on purpose: the page is served from disk on loopback.
    chunkSizeWarningLimit: 1000,
  },
  server: {
    port: 10301,
    proxy: {
      "/api": { target, changeOrigin: false },
      "/healthz": { target, changeOrigin: false },
    },
  },
});
