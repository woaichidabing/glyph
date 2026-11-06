import { defineConfig } from "vite";
import pkg from "./package.json";
import react from "@vitejs/plugin-react";

// 开发端口 5174（5173 留给 web-desktop）；/api 代理到本地后端测试台
export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  plugins: [react()],
  server: {
    port: 5174,
    // 原子写（"临时目录 + 改名"）会让 chokidar 抛 EBUSY，把整个 dev server 带崩：
    // 2026-10-12 实测，AI 改一次 src/styles/template.css 就崩一次。这些临时目录不该被监视。
    watch: {
      ignored: ["**/.*.tmpdir/**", "**/*.tmp"],
      // 轮询：本项目绝对路径含中文与全角括号，chokidar 在 Windows 上会漏事件，
      // 导致"文件改了、页面还是旧代码"（2026-11-04 真机反复出现）。
      usePolling: true,
      interval: 400,
    },
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
    },
  },
});
