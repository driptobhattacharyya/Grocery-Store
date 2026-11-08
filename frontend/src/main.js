import "bootstrap/dist/css/bootstrap.min.css";
import "bootstrap/dist/js/bootstrap.bundle.min.js";
import { createApp } from "vue";
import App from "./App.vue";
import router from "./router";
import "./assets/demo.css";
import { installHttpInterceptors } from "./http";
import { startWakeUp } from "./demo";

installHttpInterceptors();
// Free hosting sleeps: poke /api/health right away so the server is waking
// while the visitor reads the landing page.
startWakeUp();

createApp(App).use(router).mount("#app");
