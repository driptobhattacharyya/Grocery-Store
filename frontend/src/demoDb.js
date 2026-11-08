// Snapshot store behind the "Database" drawer: fetches /api/demo/db, diffs
// consecutive snapshots (a row's identity is its first column) and keeps the
// highlights that fade after a few seconds.
import { reactive, watch } from "vue";
import axios from "axios";
import { cooldownLeft, demo, errorText } from "./demo";

const HIGHLIGHT_MS = 6000;

export const db = reactive({
  tables: [],
  loaded: false,
  loading: false,
  error: "",
  /** { [table]: { [rowKey]: 'new' | 'changed' } } */
  highlight: {},
  /** { [table]: number of rows removed by the latest changes } */
  removed: {},
  /** { [table]: number of changed rows, for the tab badges } */
  changeCount: {},
});

/** Pure diff of two snapshot table lists. Exported for testing. */
export function diffSnapshots(previous, next) {
  const result = { highlight: {}, removed: {} };
  const before = new Map(previous.map((table) => [table.name, table]));
  for (const table of next) {
    const old = before.get(table.name);
    if (!old) continue;
    const oldRows = new Map(old.rows.map((row) => [String(row[0]), JSON.stringify(row)]));
    const seen = new Set();
    const marks = {};
    for (const row of table.rows) {
      const key = String(row[0]);
      seen.add(key);
      if (!oldRows.has(key)) marks[key] = "new";
      else if (oldRows.get(key) !== JSON.stringify(row)) marks[key] = "changed";
    }
    let removed = 0;
    for (const key of oldRows.keys()) if (!seen.has(key)) removed += 1;
    // A truncated table can't be diffed reliably at its edge, so only count
    // removals when both sides are complete.
    if (table.truncated || old.truncated) removed = 0;
    if (Object.keys(marks).length) result.highlight[table.name] = marks;
    if (removed) result.removed[table.name] = removed;
  }
  return result;
}

let fadeTimer = null;

function applyDiff(diff) {
  let any = false;
  for (const [name, marks] of Object.entries(diff.highlight)) {
    db.highlight[name] = { ...(db.highlight[name] || {}), ...marks };
    db.changeCount[name] = Object.keys(db.highlight[name]).length;
    any = true;
  }
  for (const [name, count] of Object.entries(diff.removed)) {
    db.removed[name] = (db.removed[name] || 0) + count;
    any = true;
  }
  if (!any) return;
  clearTimeout(fadeTimer);
  fadeTimer = setTimeout(() => {
    db.highlight = {};
    db.removed = {};
    db.changeCount = {};
  }, HIGHLIGHT_MS);
}

export function resetDb() {
  clearTimeout(fadeTimer);
  db.tables = [];
  db.loaded = false;
  db.error = "";
  db.highlight = {};
  db.removed = {};
  db.changeCount = {};
}

let inflight = null;
let rerun = false;

/**
 * Fetches a snapshot. `diff` is false for the silent baseline fetch made when
 * the toolbar appears, so the first real look has something to compare with.
 */
export function refreshDb({ diff = true } = {}) {
  if (inflight) {
    rerun = true; // the running fetch may predate the change that asked for this one
    return inflight;
  }
  db.loading = true;
  inflight = axios
    .get("/api/demo/db")
    .then((response) => {
      const tables = response.data.tables || [];
      if (diff && db.loaded) applyDiff(diffSnapshots(db.tables, tables));
      db.tables = tables;
      db.loaded = true;
      db.error = "";
    })
    .catch((error) => {
      // A 410/401 already sent the visitor back to the landing page.
      db.error = errorText(error, "Could not load the database snapshot.");
    })
    .finally(() => {
      db.loading = false;
      inflight = null;
      if (rerun) {
        rerun = false;
        refreshDb({ diff });
      }
    });
  return inflight;
}

// Auto-refresh after successful mutations while the drawer is open (debounced
// so a burst of calls produces one fetch).
let debounce = null;
watch(
  () => demo.mutations,
  () => {
    if (demo.drawer !== "db") return;
    clearTimeout(debounce);
    debounce = setTimeout(() => refreshDb(), 250);
  }
);

watch(
  () => demo.sandboxEpoch,
  () => resetDb()
);

// --- read-only SQL console state (kept here so it survives closing the drawer) ---

export const sqlConsole = reactive({
  text: "SELECT * FROM products LIMIT 10",
  running: false,
  error: "",
  result: null, // { columns, rows, truncated, elapsed_ms }
});

watch(
  () => demo.sandboxEpoch,
  () => {
    sqlConsole.error = "";
    sqlConsole.result = null;
  }
);

export const SQL_PATH = "/api/demo/sql";

export async function runSql() {
  const sql = sqlConsole.text.trim();
  if (!sql || sqlConsole.running || cooldownLeft(SQL_PATH) > 0) return;
  sqlConsole.running = true;
  sqlConsole.error = "";
  try {
    const response = await axios.post("/api/demo/sql", { sql });
    sqlConsole.result = response.data;
  } catch (error) {
    sqlConsole.result = null;
    sqlConsole.error = errorText(error, "The query could not be run.");
  } finally {
    sqlConsole.running = false;
  }
}
