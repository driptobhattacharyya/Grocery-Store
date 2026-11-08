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
});

export function resetJobs() {
  stopPolling();
  jobs.list = [];
  jobs.email = null;
  jobs.emailEnabled = false;
  jobs.messages = [];
  jobs.loaded = false;
  jobs.error = "";
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
}

/** (Re)starts or stops the poll depending on the jobs we currently know. */
export function syncPolling() {
  if (demo.drawer === "jobs" && hasActiveJobs()) {
    if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        loadJobs();
      }, 2000);
    }
  } else {
    stopPolling();
  }
}

watch(
  () => demo.drawer,
  (drawer) => {
    if (drawer === "jobs") {
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
  await loadJobs();
  return response.data.job;
}
