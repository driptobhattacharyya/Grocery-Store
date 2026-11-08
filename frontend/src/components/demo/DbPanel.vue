<template>
  <div class="db-panel">
    <ul class="nav nav-tabs db-tabs" role="tablist">
      <li v-for="table in db.tables" :key="table.name" class="nav-item" role="presentation">
        <button
          type="button"
          class="nav-link"
          :class="{ active: tab === table.name }"
          role="tab"
          @click="tab = table.name"
        >
          {{ table.name }}
          <span class="db-count">{{ table.row_count }}</span>
          <span v-if="db.changeCount[table.name]" class="db-changed" title="Rows changed">
            +{{ db.changeCount[table.name] }}
          </span>
        </button>
      </li>
      <li class="nav-item" role="presentation">
        <button type="button" class="nav-link" :class="{ active: tab === SQL }" role="tab" @click="tab = SQL">
          SQL
        </button>
      </li>
    </ul>

    <div v-if="db.error && tab !== SQL" class="alert alert-danger m-2 py-2 small" role="alert">{{ db.error }}</div>

    <!-- Table view -->
    <div v-if="tab !== SQL" class="db-view">
      <div v-if="!db.loaded && db.loading" class="text-muted small p-3">Loading snapshot…</div>
      <template v-else-if="current">
        <div class="db-meta small text-muted">
          <span>{{ current.row_count }} row{{ current.row_count === 1 ? "" : "s" }}</span>
          <span v-if="current.truncated"> · showing the first {{ current.rows.length }}</span>
          <span v-if="db.removed[current.name]" class="db-removed">
            {{ db.removed[current.name] }} row{{ db.removed[current.name] === 1 ? "" : "s" }} removed
          </span>
          <button type="button" class="btn btn-link btn-sm p-0 ms-auto" :disabled="db.loading" @click="refresh">
            {{ db.loading ? "Refreshing…" : "Refresh" }}
          </button>
        </div>
        <div class="db-scroll">
          <table class="table table-sm db-table">
            <thead>
              <tr>
                <th v-for="column in current.columns" :key="column">{{ column }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="current.rows.length === 0">
                <td :colspan="current.columns.length" class="text-muted">No rows.</td>
              </tr>
              <tr
                v-for="row in current.rows"
                :key="row[0]"
                :class="rowClass(current.name, row)"
              >
                <td v-for="(value, index) in row" :key="index" :title="String(value ?? '')">
                  <span v-if="value === null" class="text-muted fst-italic">NULL</span>
                  <template v-else>{{ value }}</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="db-legend small text-muted">
          <span class="swatch swatch-new"></span> new
          <span class="swatch swatch-changed"></span> changed since the last snapshot
        </p>
      </template>
    </div>

    <!-- SQL console -->
    <div v-else class="db-view db-sql">
      <p class="small text-muted mb-2">
        Read-only console on your private sandbox: one <code>SELECT</code> at a time, 200 rows max.
        Passwords are never returned.
      </p>
      <div class="chips mb-2">
        <button
          v-for="example in EXAMPLES"
          :key="example.label"
          type="button"
          class="chip"
          @click="useExample(example.sql)"
        >
          {{ example.label }}
        </button>
      </div>
      <textarea
        v-model="sqlConsole.text"
        class="form-control sql-input"
        rows="5"
        spellcheck="false"
        aria-label="SQL query"
        @keydown.ctrl.enter.prevent="run"
        @keydown.meta.enter.prevent="run"
      ></textarea>
      <div class="d-flex align-items-center gap-2 my-2">
        <button type="button" class="btn btn-primary btn-sm" :disabled="sqlConsole.running" @click="run">
          <span v-if="sqlConsole.running" class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
          Run
        </button>
        <span class="small text-muted">Ctrl/Cmd + Enter</span>
        <span v-if="sqlConsole.result" class="small text-muted ms-auto">
          {{ sqlConsole.result.rows.length }} row{{ sqlConsole.result.rows.length === 1 ? "" : "s" }}
          · {{ sqlConsole.result.elapsed_ms }} ms
        </span>
      </div>
      <div v-if="sqlConsole.error" class="alert alert-danger py-2 small sql-error" role="alert">
        {{ sqlConsole.error }}
      </div>
      <div v-if="sqlConsole.result" class="db-scroll">
        <p v-if="sqlConsole.result.truncated" class="small text-warning-emphasis m-1">
          Showing the first {{ sqlConsole.result.rows.length }} rows only (row cap reached).
        </p>
        <table class="table table-sm db-table">
          <thead>
            <tr>
              <th v-for="column in sqlConsole.result.columns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="sqlConsole.result.rows.length === 0">
              <td :colspan="Math.max(sqlConsole.result.columns.length, 1)" class="text-muted">No rows.</td>
            </tr>
            <tr v-for="(row, rowIndex) in sqlConsole.result.rows" :key="rowIndex">
              <td v-for="(value, index) in row" :key="index" :title="String(value ?? '')">
                <span v-if="value === null" class="text-muted fst-italic">NULL</span>
                <template v-else>{{ value }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script>
import { demo } from "@/demo";
import { db, refreshDb, runSql, sqlConsole } from "@/demoDb";

const SQL = "__sql__";

const EXAMPLES = [
  {
    label: "Top-selling products",
    sql: "SELECT product_name, SUM(quantity) AS units_sold\nFROM order_items\nGROUP BY product_name\nORDER BY units_sold DESC\nLIMIT 5",
  },
  {
    label: "Inventory below 20",
    sql: "SELECT p.name, i.quantity\nFROM inventory i\nJOIN products p ON p.productID = i.productID\nWHERE i.quantity < 20\nORDER BY i.quantity",
  },
  {
    label: "Orders per user",
    sql: "SELECT email, COUNT(*) AS orders, ROUND(SUM(total_price), 2) AS spent\nFROM orders\nGROUP BY email",
  },
  {
    label: "Pending manager requests",
    sql: "SELECT * FROM store_manager\nWHERE manager_status = 'pending'",
  },
  {
    label: "Products per category",
    sql: "SELECT c.category_name, COUNT(p.productID) AS products\nFROM categories c\nLEFT JOIN products p ON p.categoryID = c.categoryID\nGROUP BY c.categoryID",
  },
  {
    label: "Orders + items (join)",
    sql: "SELECT o.orderID, o.email, oi.product_name, oi.quantity\nFROM orders o\nJOIN order_items oi ON oi.orderID = o.orderID\nORDER BY o.orderID DESC\nLIMIT 20",
  },
];

export default {
  data() {
    return { tab: "", SQL, EXAMPLES, db, sqlConsole };
  },
  computed: {
    current() {
      return this.db.tables.find((table) => table.name === this.tab) || null;
    },
  },
  watch: {
    "db.tables"(tables) {
      if (!this.tab && tables.length) this.tab = tables[0].name;
    },
  },
  mounted() {
    demo.dbDirty = false;
    if (db.tables.length) this.tab = db.tables[0].name;
    refreshDb();
  },
  methods: {
    refresh() {
      refreshDb();
    },
    rowClass(table, row) {
      const kind = db.highlight[table] && db.highlight[table][String(row[0])];
      return kind ? `row-${kind}` : "";
    },
    useExample(sql) {
      sqlConsole.text = sql;
      runSql();
    },
    run() {
      runSql();
    },
  },
};
</script>
