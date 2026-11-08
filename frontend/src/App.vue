<template>
  <DemoToolbar v-if="showToolbar" />
  <router-view :key="viewKey" />
  <ServerStatus />
</template>

<script>
import DemoToolbar from "@/components/demo/DemoToolbar.vue";
import ServerStatus from "@/components/demo/ServerStatus.vue";
import { demo, hasSandbox } from "@/demo";

export default {
  components: { DemoToolbar, ServerStatus },
  computed: {
    // A new sandbox starts from clean page state (even on the same route).
    viewKey() {
      return demo.viewKey;
    },
    // On every page except the landing page, once a sandbox exists.
    showToolbar() {
      void demo.version;
      return this.$route.name !== "landing" && hasSandbox();
    },
  },
};
</script>

<style>
#app {
  font-family: Avenir, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-align: center;
  color: #2c3e50;
}

/* Makes the .navbar-brand in the three navbars bold (Bootstrap doesn't). */
nav a {
  font-weight: bold;
  color: #2c3e50;
}
</style>
