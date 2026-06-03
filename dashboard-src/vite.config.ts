import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/dashboard/",
  root: __dirname,
  publicDir: false,
  plugins: [
    react(),
    {
      name: "strip-dashboard-trailing-whitespace",
      generateBundle(_options, bundle) {
        for (const item of Object.values(bundle)) {
          if (item.type === "chunk") {
            item.code = item.code.replace(/[ \t]+$/gm, "");
          } else if (typeof item.source === "string") {
            item.source = item.source.replace(/[ \t]+$/gm, "");
          }
        }
      },
    },
  ],
  build: {
    chunkSizeWarningLimit: 900,
    outDir: "../luma/assets/dashboard",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "app.js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.name?.endsWith(".css")) return "styles.css";
          return "asset-[name][extname]";
        },
      },
    },
  },
});
