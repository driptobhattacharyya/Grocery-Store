<template>
  <transition name="pill-fade">
    <div v-if="visible" class="server-pill" :class="demo.server" role="status" aria-live="polite">
      <template v-if="demo.server === 'waking'">
        <span class="spinner-border spinner-border-sm" aria-hidden="true"></span>
        <span>Waking the demo server… (free hosting, up to ~1 min)</span>
      </template>
      <template v-else-if="demo.server === 'ready'">
        <span aria-hidden="true">&#10003;</span>
        <span>Server ready</span>
      </template>
      <template v-else>
        <span>Still can't reach the server. Free hosting can take a few minutes to start; check your connection.</span>
        <button type="button" @click="retry">Try again</button>
      </template>
    </div>
  </transition>
</template>

<script>
import { demo, startWakeUp } from "@/demo";

export default {
  data() {
    return { demo, showReady: false, timer: null };
  },
  computed: {
    visible() {
      return this.demo.server !== "ready" || this.showReady;
    },
  },
  watch: {
    "demo.server"(state, previous) {
      clearTimeout(this.timer);
      if (state === "ready" && previous !== "ready") {
        this.showReady = true;
        this.timer = setTimeout(() => (this.showReady = false), 3000);
      } else {
        this.showReady = false;
      }
    },
  },
  beforeUnmount() {
    clearTimeout(this.timer);
  },
  methods: {
    retry() {
      startWakeUp();
    },
  },
};
</script>
