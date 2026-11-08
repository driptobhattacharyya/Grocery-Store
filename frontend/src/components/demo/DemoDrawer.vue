<template>
  <transition name="demo-drawer">
    <aside v-if="open" class="demo-drawer" role="dialog" :aria-label="title" @keydown.esc="$emit('close')">
      <header class="demo-drawer-head">
        <h2 class="demo-drawer-title">{{ title }}</h2>
        <button type="button" class="btn-close" aria-label="Close panel" @click="$emit('close')"></button>
      </header>
      <ResetNotice v-if="demo.full" class="m-2" />
      <div class="demo-drawer-body">
        <slot />
      </div>
    </aside>
  </transition>
</template>

<script>
import ResetNotice from "./ResetNotice.vue";
import { demo } from "@/demo";

export default {
  components: { ResetNotice },
  data() {
    return { demo };
  },
  props: { open: Boolean, title: String },
  emits: ["close"],
  watch: {
    open(value) {
      if (value) document.addEventListener("keydown", this.onKey);
      else document.removeEventListener("keydown", this.onKey);
    },
  },
  beforeUnmount() {
    document.removeEventListener("keydown", this.onKey);
  },
  methods: {
    onKey(event) {
      if (event.key === "Escape") this.$emit("close");
    },
  },
};
</script>
