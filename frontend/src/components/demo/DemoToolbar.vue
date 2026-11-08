<template>
  <div class="demo-toolbar" role="region" aria-label="Demo controls">
    <div class="demo-toolbar-inner">
      <router-link to="/" class="demo-tag" title="Back to the demo home">DEMO</router-link>

      <div class="demo-menu-wrap" ref="menuWrap">
        <button
          type="button"
          class="demo-btn"
          :class="{ active: menuOpen }"
          aria-haspopup="menu"
          :aria-expanded="menuOpen"
          data-testid="role-switcher"
          @click="menuOpen = !menuOpen"
        >
          <span class="role-badge" :class="'role-' + (role || 'guest')" data-testid="role-badge">{{ roleLabel }}</span>
          <span v-if="who" class="demo-who d-none d-md-inline">{{ who }}</span>
          <span aria-hidden="true">&#9662;</span>
        </button>
        <ul v-if="menuOpen" class="demo-menu" role="menu">
          <li class="menu-label">Switch role</li>
          <li v-for="option in ROLES" :key="option.role">
            <button type="button" role="menuitem" :disabled="switching" @click="switchTo({ role: option.role })">
              {{ option.label }}
            </button>
          </li>
          <template v-if="demo.emails.length">
            <li><hr /></li>
            <li class="menu-label">Accounts you created</li>
            <li v-for="email in demo.emails" :key="email">
              <button type="button" role="menuitem" :disabled="switching" @click="switchTo({ email })">
                My account ({{ email }})
              </button>
            </li>
          </template>
        </ul>
      </div>

      <button type="button" class="demo-btn" :class="{ active: demo.drawer === 'db' }" data-testid="open-db" @click="toggle('db')">
        <span>Database</span>
        <span v-if="demo.dbDirty && demo.drawer !== 'db'" class="dot" title="Changes since you last looked" data-testid="db-dot"></span>
      </button>
      <button type="button" class="demo-btn" :class="{ active: demo.drawer === 'jobs' }" data-testid="open-jobs" @click="toggle('jobs')">
        <span>Emails <span class="label-long">&amp; jobs</span></span>
      </button>
      <button type="button" class="demo-btn" data-testid="reset-demo" @click="confirmReset = true">Reset <span class="label-long">demo</span></button>

      <span class="demo-note">Demo data resets after an hour of inactivity</span>
    </div>
  </div>

  <DemoDrawer :open="demo.drawer === 'db'" title="Database (your private sandbox)" @close="demo.drawer = null">
    <DbPanel />
  </DemoDrawer>
  <DemoDrawer :open="demo.drawer === 'jobs'" title="Emails &amp; background jobs" @close="demo.drawer = null">
    <JobsPanel />
  </DemoDrawer>

  <!-- Reset confirmation -->
  <div v-if="confirmReset" class="modal d-block" tabindex="-1" role="dialog" aria-modal="true" style="background: rgba(0,0,0,.45); z-index: 1080">
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content text-start">
        <div class="modal-header">
          <h5 class="modal-title">Reset the demo?</h5>
          <button type="button" class="btn-close" aria-label="Close" @click="confirmReset = false"></button>
        </div>
        <div class="modal-body">
          This starts a fresh sandbox as <strong>{{ roleLabel === "Guest" ? "Shopper" : roleLabel }}</strong>. Everything you
          changed in the current one (orders, products, sign-ups) is discarded.
          <div v-if="resetError" class="alert alert-danger mt-3 mb-0 py-2" role="alert">{{ resetError }}</div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" @click="confirmReset = false">Cancel</button>
          <button type="button" class="btn btn-success" :disabled="resetting" data-testid="confirm-reset" @click="reset">
            <span v-if="resetting" class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
            Reset demo
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import DemoDrawer from "./DemoDrawer.vue";
import DbPanel from "./DbPanel.vue";
import JobsPanel from "./JobsPanel.vue";
import { createSession, demo, errorText, hasSandbox, switchSession } from "@/demo";
import { db, refreshDb } from "@/demoDb";
import { getRole, homeFor } from "@/auth";
import "@/demoJobs";

const ROLES = [
  { role: "user", label: "Shopper" },
  { role: "manager", label: "Store Manager" },
  { role: "admin", label: "Admin" },
];

export default {
  components: { DemoDrawer, DbPanel, JobsPanel },
  data() {
    return {
      demo,
      ROLES,
      menuOpen: false,
      switching: false,
      confirmReset: false,
      resetting: false,
      resetError: "",
    };
  },
  computed: {
    role() {
      // demo.version / the route make this re-evaluate after login, logout and switch.
      void this.demo.version;
      void this.$route.fullPath;
      return getRole();
    },
    roleLabel() {
      return { admin: "Admin", manager: "Manager", user: "Shopper" }[this.role] || "Guest";
    },
    who() {
      void this.demo.version;
      if (this.role === "user") return window.sessionStorage.getItem("name") || "";
      return "";
    },
  },
  watch: {
    "demo.sandboxEpoch"() {
      this.baseline();
    },
  },
  mounted() {
    document.addEventListener("click", this.onDocClick);
    this.baseline();
  },
  beforeUnmount() {
    document.removeEventListener("click", this.onDocClick);
  },
  methods: {
    baseline() {
      // A silent first snapshot so the first look at the Database panel can
      // already show what changed since the session began.
      if (hasSandbox() && !db.loaded) refreshDb({ diff: false });
    },
    onDocClick(event) {
      if (this.menuOpen && this.$refs.menuWrap && !this.$refs.menuWrap.contains(event.target)) {
        this.menuOpen = false;
      }
    },
    toggle(name) {
      this.demo.drawer = this.demo.drawer === name ? null : name;
      if (this.demo.drawer === "db") this.demo.dbDirty = false;
    },
    async switchTo(target) {
      this.switching = true;
      try {
        await switchSession(target);
        this.menuOpen = false;
        const home = homeFor(getRole());
        if (this.$route.fullPath === home) this.demo.viewKey += 1; // same page, different account
        else await this.$router.push(home);
      } catch (error) {
        if (!error.response || ![401, 410].includes(error.response.status)) {
          window.alert(errorText(error, "Could not switch role."));
        }
      } finally {
        this.switching = false;
      }
    },
    async reset() {
      this.resetting = true;
      this.resetError = "";
      try {
        await createSession(this.role || "user");
        this.confirmReset = false;
        this.$router.push(homeFor(getRole()));
      } catch (error) {
        this.resetError = error.response ? errorText(error) : error.message || errorText(error);
      } finally {
        this.resetting = false;
      }
    },
  },
};
</script>
