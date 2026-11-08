import { fileURLToPath, URL } from "node:url";
import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

// In development the Flask API runs on :5000 and Vite proxies /api to it, so
// with an empty VITE_API_BASE_URL the app talks to its own origin (same as the
// Docker image). In production VITE_API_BASE_URL points at the Render service.
//
// VITE_DEMO_MOCK=true (dev server only) swaps the proxy for an in-memory fake
// of the demo API from dev-mock/. It is imported dynamically and only for
// `vite` (serve), so `vite build` never even loads it.
export default defineConfig(async ({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), ["VITE_", "MOCK_"]);
  const plugins = [vue()];
  const useMock = command === "serve" && env.VITE_DEMO_MOCK === "true";
  if (useMock) {
    const { default: demoMock } = await import("./dev-mock/demoMock.js");
    plugins.push(demoMock(env));
  }
  return {
    plugins,
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    build: {
      sourcemap: false,
    },
    server: {
      port: 8080,
      proxy: useMock ? {} : { "/api": "http://127.0.0.1:5000" },
    },
  };
});
