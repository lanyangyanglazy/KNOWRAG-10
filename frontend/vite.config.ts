import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发期把 /api 代理到 FastAPI 后端（默认 127.0.0.1:8000）。
// 用 Cookie 会话（HttpOnly / SameSite=Lax），代理后浏览器视角仍是同源，Cookie 正常回传。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
