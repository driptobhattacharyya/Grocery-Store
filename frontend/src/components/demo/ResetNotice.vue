<template>
  <div class="reset-notice" data-testid="reset-notice">
    <span>{{ demo.full }}</span>
    <button type="button" class="btn btn-sm btn-danger" @click="demo.resetRequest += 1">Reset demo</button>
  </div>
</template>

<script>
import { demo } from "@/demo";

/** "Sandbox full" message with a Reset demo action (opens the toolbar's reset dialog). */
export default {
  data() {
    return { demo };
  },
};
</script>
