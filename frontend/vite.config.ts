import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The container demo proxies /api through nginx, so the app uses relative
// paths. The dev-server proxy is a local convenience only (target = the
// compose-published backend port on 127.0.0.1).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:18080", changeOrigin: true },
    },
  },
});
