import axios from "axios";
import router from "./router";
import { getSandboxId, getToken } from "./auth";
import {
  demo,
  endSession,
  errorText,
  hasSandbox,
  isSandboxFull,
  noteMutation,
  retryAfterOf,
  startCooldown,
  startWakeUp,
  EXPIRED_MESSAGE,
  FULL_MESSAGE,
  SESSION_LOST_MESSAGE,
  WOKE_MESSAGE,
} from "./demo";

/**
 * API base URL. Empty means "same origin" (local dev through the Vite proxy,
 * and the Docker image); in production it points at the Render service.
 */
export const API_BASE = String(import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");

export function apiUrl(path) {
  return API_BASE + path;
}

// Credential endpoints legitimately answer 401 for a bad password; those must
// reach the form's own error handling without ending the session.
const AUTH_ENDPOINTS = ["/api/login", "/api/admin/login", "/api/signup"];

function pathOf(url = "") {
  return String(url)
    .replace(/^https?:\/\/[^/]+/, "")
    .split("?")[0]
    .replace(/\/+$/, "");
}

// Calls that must not be treated as "mutations" for the DB panel.
const NON_MUTATING_POSTS = ["/api/demo/sql", "/api/demo/session", "/api/demo/switch"];

/**
 * Sends the sandbox id and token with every API call and reacts to a lost
 * session:
 *   - 410 (sandbox expired/unknown): clear everything, back to the landing page
 *     with a friendly message.
 *   - 401 on a non-login endpoint (the token is rejected or belongs to another
 *     sandbox): in the demo this means the session is gone too.
 * Every successful non-GET call tells the DB panel to refresh.
 * Safe to call once; a second call is a no-op.
 */
let installed = false;
export function installHttpInterceptors(instance = axios) {
  if (installed) return;
  installed = true;
  instance.defaults.baseURL = API_BASE;

  instance.interceptors.request.use((config) => {
    // `demoAnon` calls (health check, session creation) carry no custom
    // headers, which keeps them simple CORS requests while the server wakes.
    if (config.demoAnon) return config;
    const sandboxId = getSandboxId();
    if (sandboxId) config.headers.set("X-Sandbox-Id", sandboxId);
    const token = getToken();
    if (token && !config.headers.get("Authorization")) {
      config.headers.set("Authorization", `Bearer ${token}`);
    }
    return config;
  });

  instance.interceptors.response.use(
    (response) => {
      const method = String(response.config.method || "get").toLowerCase();
      if (method !== "get" && !NON_MUTATING_POSTS.includes(pathOf(response.config.url))) {
        noteMutation();
      }
      return response;
    },
    async (error) => {
      const config = error.config;
      if (config && !config.demoAnon && !error.response && !axios.isCancel(error)) {
        // Network/CORS failure with a sandbox in play: the free Render
        // instance most likely spun down. Join the single bounded wake-up
        // loop; GETs are retried once, everything else asks the visitor to
        // repeat the action (it may or may not have reached the server).
        if (hasSandbox()) {
          demo.server = "waking";
          const awake = await startWakeUp();
          if (awake && String(config.method || "get").toLowerCase() === "get" && !config.demoRetried) {
            config.demoRetried = true;
            return instance.request(config);
          }
          if (awake) error.wakeMessage = WOKE_MESSAGE;
          if (error.wakeMessage) error.message = error.wakeMessage;
        }
        return Promise.reject(error);
      }
      if (config && !config.demoAnon && error.response) {
        const status = error.response.status;
        if (status === 429) {
          const wait = retryAfterOf(error);
          if (wait && String(config.method || "get").toLowerCase() !== "get") startCooldown(pathOf(config.url), wait);
          error.message = errorText(error);
        } else if (isSandboxFull(error)) {
          demo.full = (error.response.data && error.response.data.error) || FULL_MESSAGE;
          error.message = demo.full;
        }
      }
      if (error.config && !error.config.demoAnon) {
        const lost = sessionLost(error.response && error.response.status, pathOf(error.config.url));
        // The visitor is already being sent back to the landing page. The
        // existing pages answer a failed call with alert(error), which would
        // pop up over the landing page, so their request simply never
        // settles. The demo panels' own calls (/api/demo/*) still reject.
        if (lost && !pathOf(error.config.url).startsWith("/api/demo/")) return new Promise(() => {});
      }
      return Promise.reject(error);
    }
  );
}

/** Shared by axios errors and the fetch-based report download. */
export function sessionLost(status, path = "") {
  const expired = status === 410;
  const rejected = status === 401 && !AUTH_ENDPOINTS.includes(path);
  if (!expired && !rejected) return false;
  // Several in-flight calls can fail together; only the first one ends the
  // session, so the landing page shows a single notice.
  if (hasSandbox()) endSession(expired ? EXPIRED_MESSAGE : SESSION_LOST_MESSAGE);
  if (router.currentRoute.value.path !== "/") router.replace("/");
  return true;
}
