// Demo session layer: sandbox id, role switching, "waking the server" status.
//
// The demo backend gives every visitor a private sandbox database. The
// sandbox id lives in sessionStorage next to the auth keys managed by
// auth.js; this module adds the reactive state the UI needs on top of that.
import { reactive } from "vue";
import axios from "axios";
import { SANDBOX_KEY, getRole, getSandboxId, logout } from "./auth";

export const EXPIRED_MESSAGE =
  "Your demo session expired (they reset after an hour of inactivity or when the free server restarts). Start a new one.";
export const SESSION_LOST_MESSAGE =
  "Your demo session ended (the sign-in is no longer valid in this sandbox). Start a new one.";

/** Accounts of the seeded sandbox, shown on the landing page before a session exists. */
export const DEFAULT_ACCOUNTS = [
  { role: "user", email: "demo@grocery.app", password: "demo1234" },
  { role: "manager", email: "manager@grocery.app", password: "manager1234" },
  { role: "admin", username: "admin", password: "admin1234" },
];
const SEEDED_EMAILS = ["demo@grocery.app", "manager@grocery.app"];

const ACCOUNTS_KEY = "demo_accounts";
const EMAILS_KEY = "demo_emails";

function readJson(key, fallback) {
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function write(key, value) {
  try {
    if (value === null) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, typeof value === "string" ? value : JSON.stringify(value));
  } catch {
    /* storage unavailable: the session just won't survive a reload */
  }
}

export const demo = reactive({
  /** 'waking' | 'ready' | 'unreachable' */
  server: "waking",
  /** Message for the landing page (e.g. "your session expired"). */
  notice: "",
  /** Bumped whenever the session changes, so computed role/labels refresh. */
  version: 0,
  accounts: readJson(ACCOUNTS_KEY, null) || DEFAULT_ACCOUNTS,
  /** Accounts created in this sandbox by signing up or logging in. */
  emails: readJson(EMAILS_KEY, []),
  /** Which drawer is open: 'db' | 'jobs' | null. */
  drawer: null,
  /** Counts successful non-GET API calls; panels watch it to refresh. */
  mutations: 0,
  /** Something changed in the sandbox since the DB panel was last looked at. */
  dbDirty: false,
  /** Bumped when a new sandbox starts, so panels drop their cached data. */
  sandboxEpoch: 0,
  /** Message while the sandbox is full (507): drives the global banner. */
  full: "",
  /** Bumped by anything that wants the toolbar's "Reset demo" dialog opened. */
  resetRequest: 0,
  /** Clock for countdowns; only ticks while a cooldown is running. */
  now: Date.now(),
  /** { [key]: epoch ms until which the action is rate-limited } */
  cooldowns: {},
  /** Changing it remounts the routed page (fresh sandbox, or same-route role switch). */
  viewKey: 0,
});

export function noteMutation() {
  demo.mutations += 1;
  if (demo.drawer !== "db") demo.dbDirty = true;
}

// --- rate-limit cooldowns -----------------------------------------------------

let cooldownTimer = null;

/** Disables the action `key` (an API path) for `seconds`. */
export function startCooldown(key, seconds) {
  const secs = Math.max(1, Math.min(Math.ceil(Number(seconds) || 0), 3600));
  demo.now = Date.now();
  demo.cooldowns = { ...demo.cooldowns, [key]: demo.now + secs * 1000 };
  if (cooldownTimer) return;
  cooldownTimer = setInterval(() => {
    demo.now = Date.now();
    const live = Object.entries(demo.cooldowns).filter(([, until]) => until > demo.now);
    if (live.length !== Object.keys(demo.cooldowns).length) demo.cooldowns = Object.fromEntries(live);
    if (!live.length) {
      clearInterval(cooldownTimer);
      cooldownTimer = null;
    }
  }, 250);
}

/** Whole seconds left on the cooldown for `key` (0 when none). Reactive. */
export function cooldownLeft(key) {
  const until = demo.cooldowns[key];
  return until ? Math.max(0, Math.ceil((until - demo.now) / 1000)) : 0;
}

/** Seconds to wait according to a 429 error (body `retry_after`, else Retry-After header). */
export function retryAfterOf(error) {
  const res = error && error.response;
  if (!res) return 0;
  const fromBody = Number(res.data && res.data.retry_after);
  const header = res.headers && (typeof res.headers.get === "function" ? res.headers.get("retry-after") : res.headers["retry-after"]);
  const fromHeader = Number(header);
  const value = fromBody > 0 ? fromBody : fromHeader > 0 ? fromHeader : 0;
  return value ? Math.ceil(value) : 0;
}

export function isSandboxFull(error) {
  const res = error && error.response;
  return Boolean(res && (res.status === 507 || (res.data && res.data.code === "sandbox_full")));
}

export const WOKE_MESSAGE = "The server was asleep and is waking up. Please try that again in a moment.";
export const FULL_MESSAGE = "This demo sandbox is full. Reset the demo to start fresh.";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --- waking the server --------------------------------------------------------

const WAKE_BUDGET_MS = 90_000;
const WAKE_ATTEMPT_TIMEOUT_MS = 10_000;
let wakeRun = null;

/**
 * Calls /api/health with exponential backoff (1 s, 2 s, 4 s, then 5 s) for up
 * to ~90 s. A free Render instance that is asleep answers with network/CORS
 * errors or 502/503/504 while it boots; every failure just means "waking".
 * Resolves true when the server answered, false when the budget ran out.
 */
export function startWakeUp() {
  if (wakeRun) return wakeRun;
  demo.server = "waking";
  const started = Date.now();
  wakeRun = (async () => {
    let delay = 1000;
    for (;;) {
      try {
        await axios.get("/api/health", { timeout: WAKE_ATTEMPT_TIMEOUT_MS, demoAnon: true });
        demo.server = "ready";
        return true;
      } catch {
        /* network error, CORS failure, 502/503/504, ...: still waking */
      }
      if (Date.now() - started + delay > WAKE_BUDGET_MS) {
        demo.server = "unreachable";
        return false;
      }
      await sleep(delay);
      delay = Math.min(delay * 2, 5000);
    }
  })().finally(() => {
    wakeRun = null;
  });
  return wakeRun;
}

/** Waits for the server (starting a wake-up if none is running). */
export async function ensureReady() {
  if (demo.server === "ready") return true;
  return startWakeUp();
}

// --- session ------------------------------------------------------------------

export function errorText(error, fallback = "Something went wrong. Please try again.") {
  if (error && error.wakeMessage) return error.wakeMessage;
  if (error && error.response) {
    const data = error.response.data;
    if (error.response.status === 429) {
      const wait = retryAfterOf(error);
      return wait ? `Too many requests. Try again in ${wait} s` : "Too many requests. Please wait a bit and try again.";
    }
    if (isSandboxFull(error)) return (data && data.error) || FULL_MESSAGE;
    if (data && typeof data.error === "string") return data.error;
    return `${fallback} (HTTP ${error.response.status})`;
  }
  return "Could not reach the demo server. If it is waking up, give it a minute and try again.";
}

/**
 * Stores a /api/demo/session or /api/demo/switch response: sandbox id, token
 * and the role flags that auth.js reads.
 */
export function applySession(data, { fresh = false } = {}) {
  if (data.sandbox_id) write(SANDBOX_KEY, data.sandbox_id);
  logout(); // drop any keys left by a previous role
  write("accessToken", data.access_token);
  if (data.role === "admin") {
    write("admin", "true");
  } else if (data.is_manager || data.role === "manager") {
    write("is_manager", "true");
  } else {
    write("signed", "true");
    write("name", data.name || "");
  }
  if (data.accounts) {
    demo.accounts = data.accounts;
    write(ACCOUNTS_KEY, data.accounts);
  }
  if (fresh) {
    demo.emails = [];
    write(EMAILS_KEY, null);
    demo.dbDirty = false;
    demo.full = "";
    demo.cooldowns = {};
    demo.sandboxEpoch += 1;
    demo.viewKey += 1;
  }
  demo.notice = "";
  demo.version += 1;
}

/** Starts a brand-new sandbox as `role` ('user' | 'manager' | 'admin'). */
export async function createSession(role) {
  if (!(await ensureReady())) {
    throw new Error("The demo server did not wake up in time. Please try again in a minute.");
  }
  // The very first call after a wake-up can still hit a half-started server:
  // retry network-level failures a few times.
  let lastError;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const response = await axios.post("/api/demo/session", { role }, { demoAnon: true });
      applySession(response.data, { fresh: true });
      return response.data;
    } catch (error) {
      if (error.response && ![502, 503, 504].includes(error.response.status)) throw error;
      lastError = error;
      demo.server = "waking";
      await sleep(1000 * 2 ** attempt);
    }
  }
  startWakeUp();
  throw lastError;
}

/** Switches role (or to a signed-up account, by e-mail) inside the current sandbox. */
export async function switchSession(target) {
  const body = target.email ? { email: target.email } : { role: target.role };
  const response = await axios.post("/api/demo/switch", body);
  applySession(response.data);
  return response.data;
}

/** The demo role of the current session, or null for a guest in the sandbox. */
export function currentRole() {
  return getRole();
}

export function hasSandbox() {
  return getSandboxId() !== null;
}

/** Forgets the sandbox entirely (expiry, or the visitor left). */
export function endSession(message = "") {
  logout();
  [SANDBOX_KEY, ACCOUNTS_KEY, EMAILS_KEY, "cartItems", "totalAmount", "orderId"].forEach((key) =>
    write(key, null)
  );
  demo.emails = [];
  demo.accounts = DEFAULT_ACCOUNTS;
  demo.drawer = null;
  demo.dbDirty = false;
  demo.sandboxEpoch += 1;
  demo.full = "";
  demo.cooldowns = {};
  demo.notice = message;
  demo.version += 1;
}

/** Remembers an account created/used in this sandbox for the "My account" switcher. */
export function rememberEmail(email) {
  const clean = String(email || "").trim().toLowerCase();
  if (!clean || SEEDED_EMAILS.includes(clean) || demo.emails.includes(clean)) return;
  demo.emails = [...demo.emails, clean];
  write(EMAILS_KEY, demo.emails);
}
