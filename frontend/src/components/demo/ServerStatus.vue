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
  <div v-if="demo.wakeNote" class="server-pill ready wake-note" role="alert" data-testid="wake-note" style="bottom: 3.5rem">
    <span>{{ demo.wakeNote }}</span>
    <button type="button" @click="demo.wakeNote = ''">OK</button>
  </div>
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
    "demo.wakeNote"(note) {
      clearTimeout(this.noteTimer);
      if (note) this.noteTimer = setTimeout(() => (this.demo.wakeNote = ""), 10000);
    },
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
    clearTimeout(this.noteTimer);
  },
  methods: {
    retry() {
      startWakeUp();
    },
  },
};
</script>
