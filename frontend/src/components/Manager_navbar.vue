<template>
    <nav class="navbar navbar-expand-lg">
      <div class="container-fluid">
        <a class="navbar-brand" href="#">Welcome Manager</a>
        <button
          class="navbar-toggler"
          type="button"
          data-bs-toggle="collapse"
          data-bs-target="#navbarSupportedContent"
          aria-controls="navbarSupportedContent"
          aria-expanded="false"
          aria-label="Toggle navigation"
        >
          <span class="navbar-toggler-icon"></span>
        </button>
        <div class="collapse navbar-collapse" id="navbarSupportedContent">
          <ul class="navbar-nav me-auto mb-2 mb-lg-0">
            <li class="nav-item">
              <a
                href="#"
                class="nav-link active"
                aria-current="page"
                @click="goHome"
                >Home</a
              >
            </li>

            <li class="nav-item">
              <a
                class="nav-link active"
                aria-current="page"
                href="#"
                @click="download_report"
                >Report</a
              >
            </li>
  
            <li class="nav-item">
              <a class="nav-link" href="#" @click="logout">Logout</a>
            </li>
          </ul>
        </div>
      </div>
    </nav>
  </template>
  
  <script>
import { logout } from "@/auth";
import { downloadReport } from "@/report";

export default {
  methods: {
    goHome() {
      if (this.$route.path === "/StoreManager/dashboard") {
        window.location.reload();
      } else {
        this.$router.push("/StoreManager/dashboard");
      }
    },
    logout() {
      logout();
      alert("Logged out successfully");
      this.$router.push("/shop");
    },
    download_report() {
      return downloadReport();
    },
  },
};
</script>
  