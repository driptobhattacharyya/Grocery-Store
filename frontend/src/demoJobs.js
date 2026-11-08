// State behind the "Emails & jobs" drawer: the visitor's address, background
// jobs (polled while any is queued or running) and the outbox.
import { reactive, watch } from "vue";
import axios from "axios";
import { demo, errorText, noteMutation } from "./demo";

export const ACTIVE = ["queued", "running"];

export const jobs = reactive({
  list: [],
  email: null,
  emailEnabled: false,
  messages: [],
  loaded: false,
  error: "",
  /** Polling gave up after POLL_CAP_MS; the visitor can refresh by hand. */
  pollStopped: false,
});

const POLL_INTERVAL_MS = 2000;
export const POLL_CAP_MS = 3 * 60 * 1000;
let pollStart = 0;

export function resetJobs() {
  stopPolling();
  jobs.list = [];
  jobs.email = null;
  jobs.emailEnabled = false;
  jobs.messages = [];
  jobs.loaded = false;
  jobs.error = "";
  jobs.pollStopped = false;
}

export const hasActiveJobs = () => jobs.list.some((job) => ACTIVE.includes(job.status));

export async function loadOutbox() {
  try {
    const response = await axios.get("/api/demo/outbox");
    jobs.messages = response.data.messages || [];
  } catch (error) {
    jobs.error = errorText(error, "Could not load the outbox.");
  }
}

export async function loadJobs() {
  try {
    const response = await axios.get("/api/demo/jobs");
    const before = new Map(jobs.list.map((job) => [job.id, job.status]));
    const incoming = response.data.jobs || [];
    const progressed = incoming.some((job) => before.has(job.id) && before.get(job.id) !== job.status);
    jobs.list = incoming;
    jobs.email = response.data.email || null;
    jobs.emailEnabled = Boolean(response.data.email_enabled);
    jobs.loaded = true;
    jobs.error = "";
    if (progressed) {
      // A worker changed the jobs/outbox tables: let the DB panel know.
      noteMutation();
      loadOutbox();
    }
  } catch (error) {
    jobs.error = errorText(error, "Could not load jobs.");
  }
  syncPolling();
}

// --- polling: every 2 s while a job is queued or running ----------------------

let timer = null;

function stopPolling() {
  clearTimeout(timer);
  timer = null;
  pollStart = 0;
}

/** Manual refresh: starts a fresh polling window. */
export function refreshJobs() {
  pollStart = 0;
  jobs.pollStopped = false;
  loadOutbox();
  return loadJobs();
}

/** (Re)starts or stops the poll depending on the jobs we currently know. */
export function syncPolling() {
  if (demo.drawer === "jobs" && hasActiveJobs()) {
    if (!pollStart) pollStart = Date.now();
    if (Date.now() - pollStart >= POLL_CAP_MS) {
      // Stuck job (or a very slow worker): stop hammering the server.
      jobs.pollStopped = true;
      clearTimeout(timer);
      timer = null;
    } else if (!timer) {
      jobs.pollStopped = false;
      timer = setTimeout(() => {
        timer = null;
        loadJobs();
      }, POLL_INTERVAL_MS);
    }
  } else {
    jobs.pollStopped = false;
    stopPolling();
  }
}

watch(
  () => demo.drawer,
  (drawer) => {
    if (drawer === "jobs") {
      pollStart = 0;
      jobs.pollStopped = false;
      loadJobs();
      loadOutbox();
    } else {
      stopPolling();
    }
  }
);

watch(
  () => demo.sandboxEpoch,
  () => resetJobs()
);

export async function saveEmail(email, turnstileToken) {
  const body = { email };
  if (turnstileToken) body.turnstile_token = turnstileToken;
  const response = await axios.post("/api/demo/email", body);
  jobs.email = response.data.email || email;
  return response.data;
}

export async function enqueue(kind) {
  const response = await axios.post("/api/demo/jobs", { kind });
  pollStart = 0; // a new job gets a full polling window
  jobs.pollStopped = false;
  await loadJobs();
  return response.data.job;
}
