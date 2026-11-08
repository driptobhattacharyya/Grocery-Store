<template>
  <div class="landing">
    <header class="landing-hero">
      <div class="container">
        <div class="landing-kicker mb-2">Live portfolio demo</div>
        <h1>7 to 11 &middot; a full-stack grocery store</h1>
        <p class="mb-0">
          Pick a role and use the real app. You get a private copy of the database to play with, and you can
          watch it change while you click.
        </p>
      </div>
    </header>

    <main class="container landing-body">
      <div v-if="demo.notice" class="alert alert-warning alert-dismissible" role="alert" data-testid="expired-notice">
        {{ demo.notice }}
        <button type="button" class="btn-close" aria-label="Dismiss" @click="demo.notice = ''"></button>
      </div>
      <div v-if="error" class="alert alert-danger" role="alert">{{ error }}</div>
      <div v-if="resumable" class="alert alert-success d-flex flex-wrap align-items-center gap-2" role="status">
        <span>You already have a demo session running.</span>
        <router-link :to="resumeTo" class="btn btn-sm btn-success">Resume it</router-link>
      </div>

      <h2 class="mb-3">Explore this project as&hellip;</h2>
      <div class="row row-cols-1 row-cols-md-3 g-3 mb-5">
        <div v-for="card in CARDS" :key="card.role" class="col">
          <div class="card role-card">
            <div class="card-body d-flex flex-column">
              <div class="icon" aria-hidden="true" v-html="card.icon"></div>
              <h3 class="h5">{{ card.title }}</h3>
              <ul class="mb-3">
                <li v-for="point in card.points" :key="point">{{ point }}</li>
              </ul>
              <button
                type="button"
                class="btn btn-success mt-auto"
                :disabled="!!busy"
                :data-testid="'pick-' + card.role"
                @click="start(card.role)"
              >
                <template v-if="busy === card.role">
                  <span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>
                  {{ demo.server === "ready" ? "Starting your sandbox…" : "Waiting for the server…" }}
                </template>
                <template v-else>Explore as {{ card.title }}</template>
              </button>
            </div>
          </div>
        </div>
      </div>

      <section class="mb-5">
        <h2 class="mb-3">What's inside</h2>
        <div class="inside-grid">
          <div v-for="item in INSIDE" :key="item.name" class="inside-item">
            <strong>{{ item.name }}</strong>
            {{ item.text }}
          </div>
        </div>
      </section>

      <section class="mb-4">
        <details class="inside-item">
          <summary class="fw-semibold">Demo credentials</summary>
          <p class="small text-muted mt-2">
            The role buttons log you in automatically. These work on the regular login pages inside your sandbox.
          </p>
          <table class="table table-sm cred-table mb-0">
            <thead>
              <tr><th>Role</th><th>Login</th><th>Password</th></tr>
            </thead>
            <tbody>
              <tr v-for="account in demo.accounts" :key="account.role">
                <td>{{ roleName(account.role) }}</td>
                <td><code>{{ account.email || account.username }}</code></td>
                <td><code>{{ account.password }}</code></td>
              </tr>
            </tbody>
          </table>
        </details>
      </section>

      <p class="small text-muted">
        Source code:
        <a href="https://github.com/driptobhattacharyya/Grocery-Store" target="_blank" rel="noopener noreferrer">
          github.com/driptobhattacharyya/Grocery-Store
        </a>
        &middot; Demo data resets after an hour of inactivity.
      </p>
    </main>
  </div>
</template>

<script>
import { createSession, demo, errorText, hasSandbox } from "@/demo";
import { getRole, homeFor } from "@/auth";

const icon = (path) =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;

// Static, trusted markup (not user data).
const CARDS = [
  {
    role: "user",
    title: "Shopper",
    icon: icon('<circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/><path d="M2 3h3l2.6 12.4a1 1 0 0 0 1 .8h8.8a1 1 0 0 0 1-.8L20 8H6"/>'),
    points: ["Browse and search the catalog", "Fill a cart and check out", "See your order history"],
  },
  {
    role: "manager",
    title: "Store Manager",
    icon: icon('<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>'),
    points: ["Manage products and inventory", "Download the sales report (CSV)", "Edit prices, stock and dates"],
  },
  {
    role: "admin",
    title: "Admin",
    icon: icon('<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>'),
    points: ["Manage categories", "Approve manager requests", "Everything a manager can do"],
  },
];

const INSIDE = [
  { name: "Flask", text: "REST API with a service layer and input validation." },
  { name: "SQLite", text: "One private database copy per visitor." },
  { name: "JWT + RBAC", text: "Shopper, manager and admin roles enforced on the server." },
  { name: "Celery + Redis", text: "Background jobs that render and send e-mails." },
  { name: "Vue 3", text: "Vite single-page app with Vue Router and Bootstrap 5." },
  { name: "Cloudflare Workers + Render", text: "Static front end on the edge, API on a free web service." },
];

export default {
  data() {
    return { demo, CARDS, INSIDE, busy: "", error: "" };
  },
  computed: {
    resumable() {
      void demo.version;
      return hasSandbox() && getRole() !== null;
    },
    resumeTo() {
      void demo.version;
      return homeFor(getRole());
    },
  },
  methods: {
    roleName: (role) => ({ user: "Shopper", manager: "Store Manager", admin: "Admin" }[role] || role),
    async start(role) {
      this.error = "";
      this.busy = role;
      try {
        await createSession(role);
        await this.$router.push(homeFor(getRole()));
      } catch (error) {
        this.error = error.response ? errorText(error, "Could not start a demo session.") : error.message;
      } finally {
        this.busy = "";
      }
    },
  },
};
</script>
