<template>
  <div class="jobs-panel">
    <section class="jobs-section">
      <h3 class="jobs-h">1. Where should e-mails go?</h3>
      <form class="d-flex flex-wrap gap-2" @submit.prevent="save">
        <input
          v-model="emailInput"
          type="email"
          class="form-control form-control-sm flex-grow-1"
          placeholder="you@example.com"
          aria-label="Your e-mail address"
          aria-describedby="email-error"
          :aria-invalid="emailError ? 'true' : 'false'"
          autocomplete="email"
          maxlength="254"
          required
          data-testid="email-input"
        />
        <button
          type="submit"
          class="btn btn-primary btn-sm"
          data-testid="email-save"
          :disabled="saving || emailWait > 0 || (turnstileEnabled && !turnstileToken)"
        >
          <span v-if="saving" class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
          {{ emailWait > 0 ? `Wait ${emailWait} s` : jobs.email ? "Change" : "Save address" }}
        </button>
      </form>
      <div v-if="emailError" id="email-error" class="field-error mt-1" role="alert" data-testid="email-error">{{ emailError }}</div>
      <div v-if="turnstileEnabled" ref="turnstile" class="mt-2"></div>
      <p v-if="jobs.email" class="small mt-2 mb-0 text-success-emphasis">
        Reports will go to <strong>{{ jobs.email }}</strong>.
      </p>
      <p class="small text-muted mt-2 mb-0">Emails are sent only to the address you enter, max 5/day.</p>
    </section>

    <section class="jobs-section">
      <h3 class="jobs-h">2. Run a background job</h3>
      <p class="small text-muted">
        These run in a Celery worker (Redis queue). The worker renders the e-mail from your sandbox's data.
      </p>
      <div class="d-flex flex-wrap gap-2">
        <button type="button" class="btn btn-outline-success btn-sm" data-testid="job-monthly" :disabled="!jobs.email || !!starting || jobWait > 0" @click="start('monthly_report')">
          <span v-if="starting === 'monthly_report'" class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
          Send me a monthly report
        </button>
        <button type="button" class="btn btn-outline-success btn-sm" data-testid="job-daily" :disabled="!jobs.email || !!starting || jobWait > 0" @click="start('daily_reminder')">
          <span v-if="starting === 'daily_reminder'" class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
          Send today's reminder
        </button>
      </div>
      <p v-if="jobWait > 0" class="small text-muted mt-2 mb-0" data-testid="job-wait">Too many requests. Try again in {{ jobWait }} s</p>
      <p v-if="!jobs.email" class="small text-muted mt-2 mb-0">Save an address first.</p>
      <div v-if="jobError" class="alert alert-danger py-2 small mt-2 mb-0" role="alert">{{ jobError }}</div>
      <div v-if="jobs.loaded && !jobs.emailEnabled" data-testid="preview-note" class="alert alert-info py-2 small mt-2 mb-0" role="note">
        <strong>Preview mode:</strong> e-mails are rendered but not delivered on this deployment.
        Jobs still run for real, and the rendered message lands in the outbox below.
      </div>
    </section>

    <section class="jobs-section">
      <h3 class="jobs-h">
        Jobs
        <span v-if="polling" class="spinner-border spinner-border-sm text-secondary ms-1" title="Updating every 2 s"></span>
      </h3>
      <p v-if="jobs.loaded && jobs.list.length === 0" class="small text-muted mb-0">No jobs yet.</p>
      <ul class="job-list list-unstyled mb-0">
        <li v-for="job in jobs.list" :key="job.id" class="job-row">
          <div class="d-flex align-items-center gap-2">
            <span class="badge" :class="badgeClass(job.status)" :data-status="job.status">
              <span v-if="job.status === 'running'" class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
              {{ job.status }}
            </span>
            <strong class="small">{{ kindLabel(job.kind) }}</strong>
            <span class="small text-muted ms-auto">#{{ job.id }}</span>
          </div>
          <div class="small text-muted">
            to {{ job.to_email || "—" }} · {{ job.updated_at || job.created_at }}
          </div>
          <div v-if="job.detail" class="small text-muted job-detail" :class="{ 'is-failed': job.status === 'failed' }">{{ job.detail }}</div>
        </li>
      </ul>
      <div v-if="jobs.pollStopped" class="alert alert-warning py-2 small mt-2 mb-0" role="status" data-testid="poll-stopped">
        Stopped auto-refreshing after 3 minutes; a job still looks busy.
        <button type="button" class="btn btn-link btn-sm p-0 align-baseline" @click="refreshJobs">Refresh</button>
      </div>
      <div v-if="jobs.error" class="alert alert-danger py-2 small mt-2 mb-0" role="alert">{{ jobs.error }}</div>
    </section>

    <section class="jobs-section">
      <h3 class="jobs-h">Outbox</h3>
      <p v-if="jobs.messages.length === 0" class="small text-muted mb-0">Nothing rendered yet.</p>
      <ul class="list-unstyled mb-2">
        <li v-for="message in jobs.messages" :key="message.id">
          <button
            type="button"
            class="outbox-item"
            :class="{ active: selected && selected.id === message.id }"
            @click="selectedId = message.id"
          >
            <span class="fw-semibold">{{ message.subject }}</span>
            <span class="small text-muted d-block">to {{ message.to_email }} · {{ message.created_at }}</span>
            <span v-if="message.truncated" class="small outbox-truncated d-block">Preview truncated</span>
          </button>
        </li>
      </ul>
      <div v-if="selected" class="outbox-preview">
        <div class="small text-muted mb-1">Preview (sandboxed: scripts and links are disabled)</div>
        <div v-if="selected.truncated" class="small outbox-truncated mb-1" data-testid="outbox-truncated">
          Preview truncated: this message was cut to fit the size limit.
        </div>
        <!-- sandbox="" blocks scripts, forms, popups and same-origin access. -->
        <iframe
          class="outbox-frame"
          sandbox=""
          :srcdoc="selected.html"
          :title="'E-mail preview: ' + selected.subject"
        ></iframe>
      </div>
    </section>
  </div>
</template>

<script>
import { cooldownLeft, demo, errorText } from "@/demo";
import { enqueue, hasActiveJobs, jobs, loadJobs, loadOutbox, refreshJobs, saveEmail } from "@/demoJobs";

const SITE_KEY = import.meta.env.VITE_TURNSTILE_SITE_KEY || "";
const TURNSTILE_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

let turnstileLoading = null;
function loadTurnstile() {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (!turnstileLoading) {
    turnstileLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = TURNSTILE_SRC;
      script.async = true;
      script.onload = () => resolve(window.turnstile);
      script.onerror = () => {
        turnstileLoading = null;
        reject(new Error("Could not load the bot check."));
      };
      document.head.appendChild(script);
    });
  }
  return turnstileLoading;
}

const KIND_LABELS = {
  monthly_report: "Monthly report",
  daily_reminder: "Daily reminder",
  order_confirmation: "Order confirmation",
};

export default {
  data() {
    return {
      jobs,
      emailInput: jobs.email || "",
      saving: false,
      emailError: "",
      jobError: "",
      starting: "",
      selectedId: null,
      turnstileEnabled: Boolean(SITE_KEY),
      turnstileToken: "",
      widgetId: null,
    };
  },
  computed: {
    emailWait() {
      return cooldownLeft("/api/demo/email");
    },
    jobWait() {
      return cooldownLeft("/api/demo/jobs");
    },
    polling() {
      return hasActiveJobs() && demo.drawer === "jobs" && !jobs.pollStopped;
    },
    selected() {
      if (!jobs.messages.length) return null;
      return jobs.messages.find((m) => m.id === this.selectedId) || jobs.messages[0];
    },
  },
  watch: {
    "jobs.email"(email) {
      if (email && !this.emailInput) this.emailInput = email;
    },
  },
  async mounted() {
    if (this.turnstileEnabled) {
      try {
        const turnstile = await loadTurnstile();
        this.widgetId = turnstile.render(this.$refs.turnstile, {
          sitekey: SITE_KEY,
          callback: (token) => (this.turnstileToken = token),
          "expired-callback": () => (this.turnstileToken = ""),
        });
      } catch (error) {
        this.emailError = error.message;
      }
    }
  },
  beforeUnmount() {
    if (this.widgetId !== null && window.turnstile) window.turnstile.remove(this.widgetId);
  },
  methods: {
    refreshJobs,
    kindLabel: (kind) => KIND_LABELS[kind] || kind,
    badgeClass(status) {
      return (
        {
          queued: "text-bg-secondary",
          running: "text-bg-primary",
          sent: "text-bg-success",
          preview: "text-bg-info",
          failed: "text-bg-danger",
        }[status] || "text-bg-light"
      );
    },
    async save() {
      this.emailError = "";
      this.saving = true;
      try {
        await saveEmail(this.emailInput.trim(), this.turnstileToken);
        await loadJobs();
      } catch (error) {
        this.emailError = errorText(error, "Could not save the address.");
      } finally {
        this.saving = false;
        if (this.widgetId !== null && window.turnstile) {
          window.turnstile.reset(this.widgetId);
          this.turnstileToken = "";
        }
      }
    },
    async start(kind) {
      this.jobError = "";
      this.starting = kind;
      try {
        await enqueue(kind);
        loadOutbox();
      } catch (error) {
        this.jobError = errorText(error, "Could not start the job.");
      } finally {
        this.starting = "";
      }
    },
  },
};
</script>
