// DEV-ONLY mock of the Flask demo API, served by the Vite dev server.
//
// Enabled with VITE_DEMO_MOCK=true and only for `vite` (serve): vite.config.js
// imports this file dynamically in that case and never for `vite build`, and
// nothing under src/ imports it, so it cannot reach the production bundle.
//
// Each sandbox is a real in-memory SQLite database (node:sqlite) using the
// backend's schema, so the Database drawer and SQL console behave like the real
// thing. Extra knobs for testing the UI:
//   MOCK_WAKE_SECONDS=8      health answers 503 for this long after start (default 6)
//   MOCK_EMAIL_ENABLED=true  jobs end as "sent" instead of "preview"
//   POST /__mock/expire      forget every sandbox (next call answers 410)
//   POST /__mock/sleep?s=10  go back to "waking" for 10 s (503 on health)
import { DatabaseSync } from "node:sqlite";
import { randomBytes } from "node:crypto";

const SCHEMA = `
CREATE TABLE users (email TEXT NOT NULL UNIQUE PRIMARY KEY, password TEXT NOT NULL);
CREATE TABLE user_profiles (email TEXT NOT NULL PRIMARY KEY REFERENCES users(email), name TEXT NOT NULL, address TEXT, phone TEXT);
CREATE TABLE store_manager (email TEXT PRIMARY KEY, manager_status TEXT);
CREATE TABLE managers (email TEXT NOT NULL PRIMARY KEY, password TEXT NOT NULL);
CREATE TABLE categories (categoryID INTEGER PRIMARY KEY AUTOINCREMENT, category_name TEXT NOT NULL);
CREATE TABLE products (productID INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, manufacture_date TEXT NOT NULL,
  expiry_date TEXT NOT NULL, price REAL NOT NULL, dynamic_price REAL, is_recommended INTEGER, categoryID INTEGER NOT NULL, unit TEXT NOT NULL);
CREATE TABLE inventory (inventoryId INTEGER PRIMARY KEY AUTOINCREMENT, productID INTEGER NOT NULL UNIQUE, quantity INTEGER);
CREATE TABLE cart (cartID INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, isPurchased INTEGER DEFAULT 1);
CREATE TABLE orders (orderID INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, order_date TEXT, total_price NUMERIC);
CREATE TABLE order_items (order_itemID INTEGER PRIMARY KEY AUTOINCREMENT, orderID INTEGER NOT NULL, product_name TEXT NOT NULL, quantity INTEGER);
CREATE TABLE coupons (coupon_code TEXT NOT NULL UNIQUE PRIMARY KEY, discount_percentage REAL NOT NULL);
CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, status TEXT NOT NULL, to_email TEXT, created_at TEXT, updated_at TEXT, detail TEXT);
CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, to_email TEXT, subject TEXT, html TEXT, created_at TEXT);
`;
const TABLE_ORDER = ["users", "user_profiles", "store_manager", "managers", "categories", "products", "inventory",
  "cart", "orders", "order_items", "coupons", "jobs", "outbox"];

const CATALOG = [
  ["Fruits & Vegetables", [["Apple", 180, "kg", 14, 60], ["Banana", 48, "kg", 5, 132], ["Carrot", 40, "kg", 10, 58],
    ["Pineapple", 70, "piece", 7, 19], ["Strawberry", 150, "pack", 4, 9]]],
  ["Cold Drinks", [["Pepsi", 40, "bottle", 180, 97], ["Sprite", 40, "bottle", 180, 8]]],
  ["Dairy", [["Milk", 54, "litre", 3, 120], ["Paneer", 90, "pack", 7, 50]]],
  ["Poultry", [["Chicken", 260, "kg", 3, 44], ["Egg", 7, "piece", 14, 1445]]],
  ["Snacks", [["Maggi", 14, "packet", 270, 50], ["Munch", 10, "packet", 120, 499], ["Top Ramen", 35, "packet", 270, 400]]],
];
const ACCOUNTS = [
  { role: "user", email: "demo@grocery.app", password: "demo1234" },
  { role: "manager", email: "manager@grocery.app", password: "manager1234" },
  { role: "admin", username: "admin", password: "admin1234" },
];
const MASK = "••••••";
const iso = (offsetDays = 0) => new Date(Date.now() + offsetDays * 864e5).toISOString().slice(0, 10);
const stamp = () => new Date().toISOString().slice(0, 19).replace("T", " ");

function seed(db) {
  db.exec(SCHEMA);
  const user = db.prepare("INSERT INTO users VALUES (?, ?)");
  const profile = db.prepare("INSERT INTO user_profiles VALUES (?, ?, ?, ?)");
  user.run("demo@grocery.app", "pbkdf2:hash-demo");
  profile.run("demo@grocery.app", "Demo Shopper", "221B Baker Street", "1234567890");
  user.run("manager@grocery.app", "pbkdf2:hash-manager");
  profile.run("manager@grocery.app", "Demo Manager", "742 Evergreen Terrace", "9876543210");
  db.prepare("INSERT INTO store_manager VALUES (?, ?)").run("manager@grocery.app", "approved");
  db.prepare("INSERT INTO managers VALUES (?, ?)").run("admin", "pbkdf2:hash-admin");
  db.prepare("INSERT INTO coupons VALUES (?, ?)").run("FIRST50", 50);
  db.prepare("INSERT INTO coupons VALUES (?, ?)").run("SAVE20", 20);
  for (const [category, products] of CATALOG) {
    const { lastInsertRowid: cid } = db.prepare("INSERT INTO categories (category_name) VALUES (?)").run(category);
    for (const [name, price, unit, shelf, stock] of products) {
      const { lastInsertRowid: pid } = db.prepare(
        "INSERT INTO products (name, manufacture_date, expiry_date, price, dynamic_price, is_recommended, categoryID, unit) VALUES (?,?,?,?,?,?,?,?)"
      ).run(name, iso(-2), iso(shelf), price, price, 0, cid, unit);
      db.prepare("INSERT INTO inventory (productID, quantity) VALUES (?, ?)").run(pid, stock);
    }
  }
  // A little order history so the tables and reports are not empty.
  const order = db.prepare("INSERT INTO orders (email, order_date, total_price) VALUES (?,?,?)");
  const item = db.prepare("INSERT INTO order_items (orderID, product_name, quantity) VALUES (?,?,?)");
  let id = order.run("demo@grocery.app", iso(-9), 326).lastInsertRowid;
  item.run(id, "Apple", 1); item.run(id, "Milk", 2); item.run(id, "Egg", 6);
  id = order.run("demo@grocery.app", iso(-3), 540).lastInsertRowid;
  item.run(id, "Chicken", 2); item.run(id, "Maggi", 1); item.run(id, "Pepsi", 1);
}

export default function demoMockPlugin(env = {}) {
  const sandboxes = new Map();
  let wakeUntil = Date.now() + Number(env.MOCK_WAKE_SECONDS ?? 6) * 1000;
  const emailEnabled = String(env.MOCK_EMAIL_ENABLED) === "true";
  const TTL = 60 * 60 * 1000;

  const send = (res, status, body, headers = {}) => {
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    for (const [k, v] of Object.entries(headers)) res.setHeader(k, v);
    res.end(JSON.stringify(body));
  };
  const fail = (res, status, error, extra = {}) => send(res, status, { error, ...extra });

  function sandbox(req) {
    const id = req.headers["x-sandbox-id"];
    const sb = id && sandboxes.get(id);
    if (!sb || Date.now() - sb.last > TTL) {
      sandboxes.delete(id);
      return null;
    }
    sb.last = Date.now();
    return sb;
  }

  const rows = (sb, sql, ...params) => sb.db.prepare(sql).all(...params);
  const one = (sb, sql, ...params) => sb.db.prepare(sql).get(...params);
  const run = (sb, sql, ...params) => sb.db.prepare(sql).run(...params);

  function tokenFor(sb, role, email) {
    return `mock.${sb.id}.${role}.${Buffer.from(email).toString("base64url")}`;
  }
  function auth(req, sb) {
    const raw = String(req.headers.authorization || "").replace(/^Bearer /, "");
    const [prefix, sbx, role, mail] = raw.split(".");
    if (prefix !== "mock" || sbx !== sb.id) return null;
    return { role, email: Buffer.from(mail || "", "base64url").toString() };
  }

  function sessionPayload(sb, role, email, withAccounts) {
    const profile = one(sb, "SELECT name FROM user_profiles WHERE email = ?", email);
    const body = {
      sandbox_id: sb.id, expires_in: 3600, role,
      access_token: tokenFor(sb, role, email),
      name: role === "admin" ? "Admin" : profile?.name || email,
      is_manager: role === "manager",
    };
    if (withAccounts) body.accounts = ACCOUNTS;
    return body;
  }
  const isApprovedManager = (sb, email) =>
    Boolean(one(sb, "SELECT 1 FROM store_manager WHERE email = ? AND manager_status = 'approved'", email));

  // --- jobs ---------------------------------------------------------------
  function enqueueJob(sb, kind) {
    const { lastInsertRowid: id } = run(sb,
      "INSERT INTO jobs (kind, status, to_email, created_at, updated_at, detail) VALUES (?, 'queued', ?, ?, ?, ?)",
      kind, sb.visitorEmail, stamp(), stamp(), "waiting for a Celery worker");
    const setStatus = (status, detail) =>
      sandboxes.has(sb.id) && run(sb, "UPDATE jobs SET status = ?, updated_at = ?, detail = ? WHERE id = ?", status, stamp(), detail, id);
    setTimeout(() => setStatus("running", "rendering the e-mail"), 2500);
    setTimeout(() => {
      if (!sandboxes.has(sb.id)) return;
      const subject = { monthly_report: "Your monthly grocery report", daily_reminder: "Today's reminder: restock check",
        order_confirmation: "Your order is confirmed" }[kind] || kind;
      const lines = rows(sb, "SELECT product_name, SUM(quantity) q FROM order_items GROUP BY product_name ORDER BY q DESC LIMIT 5");
      const html = `<!doctype html><html><body style="font-family:Arial,sans-serif;color:#2c3e50"><h2 style="color:#198754">${subject}</h2>` +
        `<ul>${lines.map((l) => `<li>${String(l.product_name).replace(/[<&]/g, "")} &times; ${l.q}</li>`).join("")}</ul>` +
        `<p style="color:#888;font-size:12px">Demo e-mail from the Grocery Store portfolio project.</p></body></html>`;
      run(sb, "INSERT INTO outbox (job_id, to_email, subject, html, created_at) VALUES (?,?,?,?,?)", id, sb.visitorEmail, subject, html, stamp());
      setStatus(emailEnabled ? "sent" : "preview", emailEnabled ? "delivered" : "rendered, not delivered (no provider)");
    }, 5500);
    return one(sb, "SELECT * FROM jobs WHERE id = ?", id);
  }

  // --- handlers -----------------------------------------------------------
  async function handle(req, res, url, body) {
    const path = url.pathname.replace(/\/+$/, "");
    const m = req.method;

    if (path === "/__mock/expire") { sandboxes.clear(); return send(res, 200, { ok: true }); }
    if (path === "/__mock/sleep") { wakeUntil = Date.now() + Number(url.searchParams.get("s") || 10) * 1000; return send(res, 200, { ok: true }); }
    if (path === "/api/health") {
      return Date.now() < wakeUntil ? fail(res, 503, "Service waking") : send(res, 200, { status: "ok" });
    }
    if (path === "/api/demo/session" && m === "POST") {
      if (!["user", "manager", "admin"].includes(body.role)) return fail(res, 400, "role must be user, manager or admin");
      const db = new DatabaseSync(":memory:");
      seed(db);
      const sb = { id: randomBytes(24).toString("base64url"), db, last: Date.now(), visitorEmail: null };
      sandboxes.set(sb.id, sb);
      const email = { user: "demo@grocery.app", manager: "manager@grocery.app", admin: "admin" }[body.role];
      return send(res, 201, sessionPayload(sb, body.role, email, true));
    }

    // Everything else needs a live sandbox.
    const sb = sandbox(req);
    if (!sb) return fail(res, 410, "This demo session has expired", { code: "sandbox_expired" });
    const who = auth(req, sb);
    const needUser = () => (who ? who : (fail(res, 401, "Missing or invalid token"), null));
    const needStaff = () => {
      const u = needUser();
      if (u && !["manager", "admin"].includes(u.role)) { fail(res, 403, "Manager access required"); return null; }
      return u;
    };
    const needAdmin = () => {
      const u = needUser();
      if (u && u.role !== "admin") { fail(res, 403, "Admin access required"); return null; }
      return u;
    };

    switch (`${m} ${path}`) {
      case "POST /api/demo/switch": {
        if (body.email) {
          const e = String(body.email).toLowerCase();
          if (!one(sb, "SELECT 1 FROM users WHERE email = ?", e)) return fail(res, 404, "No such account in this sandbox");
          return send(res, 200, sessionPayload(sb, isApprovedManager(sb, e) ? "manager" : "user", e));
        }
        if (!["user", "manager", "admin"].includes(body.role)) return fail(res, 400, "bad role");
        const email = { user: "demo@grocery.app", manager: "manager@grocery.app", admin: "admin" }[body.role];
        return send(res, 200, sessionPayload(sb, body.role, email));
      }
      case "GET /api/demo/db": {
        const tables = TABLE_ORDER.map((name) => {
          const cols = rows(sb, `PRAGMA table_info(${name})`).map((c) => c.name);
          const total = one(sb, `SELECT COUNT(*) n FROM ${name}`).n;
          const data = rows(sb, `SELECT * FROM ${name} LIMIT 200`).map((r) =>
            cols.map((c) => (c === "password" ? MASK : r[c])));
          return { name, columns: cols, rows: data, row_count: total, truncated: total > 200 };
        });
        return send(res, 200, { tables });
      }
      case "POST /api/demo/sql": {
        const sql = String(body.sql || "").trim().replace(/;+\s*$/, "");
        if (!sql) return fail(res, 400, "Empty query", { code: "sql_error" });
        if (sql.includes(";")) return fail(res, 400, "Only one statement is allowed", { code: "sql_error" });
        if (!/^(select|with)\b/i.test(sql) || /\b(attach|pragma|insert|update|delete|drop|create|alter|replace|vacuum)\b/i.test(sql)) {
          return fail(res, 400, "Only read-only SELECT queries are allowed", { code: "sql_error" });
        }
        const t0 = Date.now();
        try {
          sb.db.exec("PRAGMA query_only = ON");
          const stmt = sb.db.prepare(sql);
          const cols = stmt.columns().map((c) => c.name);
          const all = stmt.all();
          const out = all.slice(0, 200).map((r) => Object.values(r).map((v, i) => (cols[i] === "password" ? null : v)));
          return send(res, 200, { columns: cols, rows: out, truncated: all.length > 200, elapsed_ms: Date.now() - t0 });
        } catch (e) {
          return fail(res, 400, String(e.message || e), { code: "sql_error" });
        } finally {
          sb.db.exec("PRAGMA query_only = OFF");
        }
      }
      case "POST /api/demo/email": {
        const e = String(body.email || "").trim();
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)) return fail(res, 400, "Please enter a valid e-mail address");
        sb.visitorEmail = e;
        return send(res, 200, { email: e });
      }
      case "POST /api/demo/jobs": {
        if (!sb.visitorEmail) return fail(res, 400, "Set your e-mail address first");
        if (!["monthly_report", "daily_reminder"].includes(body.kind)) return fail(res, 400, "unknown job kind");
        const count = rows(sb, "SELECT id FROM jobs").length;
        if (count >= 5) return fail(res, 429, "Limit reached: 5 e-mails/day per address");
        return send(res, 202, { job: enqueueJob(sb, body.kind) });
      }
      case "GET /api/demo/jobs":
        return send(res, 200, { jobs: rows(sb, "SELECT * FROM jobs ORDER BY id DESC"), email: sb.visitorEmail, email_enabled: emailEnabled });
      case "GET /api/demo/outbox":
        return send(res, 200, { messages: rows(sb, "SELECT * FROM outbox ORDER BY id DESC") });

      // ---- catalog (public) ----
      case "GET /api/user_dashboard":
        return send(res, 200, { products: productRows(sb, "1=1") });
      case "GET /api/getCategories":
        return send(res, 200, rows(sb, "SELECT categoryID, category_name FROM categories").map((c) => [c.categoryID, c.category_name]));
      case "POST /api/search": {
        const q = String(body.search_query || "").toLowerCase();
        const where = ["LOWER(name) LIKE ?", "price >= ?"];
        const params = [`%${q}%`, Number(body.min_price) || 0];
        if (Number(body.max_price)) { where.push("price <= ?"); params.push(Number(body.max_price)); }
        if (body.exclude_out_of_stock) where.push("productID IN (SELECT productID FROM inventory WHERE quantity > 0)");
        const products = rows(sb,
          `SELECT productID, name, manufacture_date, expiry_date, price, dynamic_price, is_recommended, categoryID, unit,
                  COALESCE((SELECT quantity FROM inventory i WHERE i.productID = products.productID), 0) AS stock
           FROM products WHERE ${where.join(" AND ")}`, ...params);
        return send(res, 200, { products });
      }

      // ---- accounts ----
      case "POST /api/signup": {
        const e = String(body.email || "").trim().toLowerCase();
        if (!e || !body.password || !body.name) return fail(res, 400, "Name, e-mail and password are required");
        if (one(sb, "SELECT 1 FROM users WHERE email = ?", e)) return fail(res, 409, "An account with this e-mail already exists");
        run(sb, "INSERT INTO users VALUES (?, ?)", e, "pbkdf2:" + randomBytes(6).toString("hex"));
        run(sb, "INSERT INTO user_profiles VALUES (?,?,?,?)", e, body.name, body.address || "", body.phoneNo || "");
        sb.passwords = { ...(sb.passwords || {}), [e]: body.password };
        return send(res, 201, { message: "Success" });
      }
      case "POST /api/login": {
        const e = String(body.email || "").trim().toLowerCase();
        const known = { "demo@grocery.app": "demo1234", "manager@grocery.app": "manager1234", ...(sb.passwords || {}) };
        if (!known[e] || known[e] !== body.password) return fail(res, 401, "Invalid e-mail or password");
        const role = isApprovedManager(sb, e) ? "manager" : "user";
        return send(res, 200, { access_token: tokenFor(sb, role, e), name: one(sb, "SELECT name FROM user_profiles WHERE email = ?", e)?.name || e, is_manager: role === "manager" });
      }
      case "POST /api/admin/login":
        if (body.email !== "admin" || body.password !== "admin1234") return fail(res, 401, "Invalid admin credentials");
        return send(res, 200, { access_token: tokenFor(sb, "admin", "admin") });
      case "GET /api/dashboard": {
        const u = needUser(); if (!u) return;
        const info = one(sb, "SELECT email, name, address, phone AS phone_no FROM user_profiles WHERE email = ?", u.email);
        if (!info) return fail(res, 404, "User profile not found");
        const orders = rows(sb, "SELECT orderID, order_date, total_price FROM orders WHERE email = ? ORDER BY order_date DESC, orderID DESC", u.email)
          .map((o) => ({ order_id: o.orderID, order_date: o.order_date, total_price: o.total_price,
            items: rows(sb, "SELECT product_name, quantity FROM order_items WHERE orderID = ?", o.orderID) }));
        const st = one(sb, "SELECT manager_status FROM store_manager WHERE email = ?", u.email);
        const label = { pending: "Pending", approved: "Approved", denied: "Denied" }[st?.manager_status] || "Not Applied";
        return send(res, 200, { user_info: info, user_orders: orders, manager_status: label });
      }
      case "POST /api/apply_manager": {
        const u = needUser(); if (!u) return;
        run(sb, "INSERT OR REPLACE INTO store_manager VALUES (?, 'pending')", u.email);
        return send(res, 200, { success: "Applied for manager status." });
      }

      // ---- cart ----
      case "GET /api/shopping_cart": {
        const u = needUser(); if (!u) return;
        return send(res, 200, { cart: rows(sb,
          "SELECT p.name, p.price, c.quantity, p.productID FROM cart c JOIN products p ON c.product_id = p.productID WHERE c.email = ?", u.email) });
      }
      case "POST /api/add_to_cart": {
        const u = needUser(); if (!u) return;
        const qty = Number(body.quantity);
        if (!Number.isInteger(qty) || qty <= 0) return fail(res, 400, "Quantity must be a positive whole number");
        const row = one(sb, "SELECT quantity FROM cart WHERE email = ? AND product_id = ?", u.email, body.product_id);
        if (row) run(sb, "UPDATE cart SET quantity = quantity + ? WHERE email = ? AND product_id = ?", qty, u.email, body.product_id);
        else run(sb, "INSERT INTO cart (email, product_id, quantity) VALUES (?,?,?)", u.email, body.product_id, qty);
        return send(res, 200, { success: "Product added to the cart successfully" });
      }
      case "POST /api/shopping_cart/update_product": {
        const u = needUser(); if (!u) return;
        const qty = Number(body.quantity);
        if (qty === 0) run(sb, "DELETE FROM cart WHERE email = ? AND product_id = ?", u.email, body.product_id);
        else run(sb, "UPDATE cart SET quantity = ? WHERE email = ? AND product_id = ?", qty, u.email, body.product_id);
        return send(res, 200, { success: "Product quantity updated successfully." });
      }
      case "POST /api/shopping_cart/delete_product": {
        const u = needUser(); if (!u) return;
        run(sb, "DELETE FROM cart WHERE email = ? AND product_id = ?", u.email, body.product_id);
        return send(res, 200, { success: "Product deleted successfully." });
      }
      case "POST /api/shopping_cart/checkout": {
        const u = needUser(); if (!u) return;
        const cart = rows(sb, `SELECT p.productID, p.name, p.price, c.quantity, COALESCE(i.quantity,0) stock
          FROM cart c JOIN products p ON c.product_id = p.productID LEFT JOIN inventory i ON i.productID = p.productID WHERE c.email = ?`, u.email);
        if (!cart.length) return fail(res, 400, "Your cart is empty");
        for (const it of cart) if (it.quantity > it.stock) return fail(res, 409, `Not enough stock for '${it.name}'. Available: ${it.stock}`);
        let total = 0;
        for (const it of cart) { run(sb, "UPDATE inventory SET quantity = quantity - ? WHERE productID = ?", it.quantity, it.productID); total += it.price * it.quantity; }
        total = Math.round(total * 100) / 100;
        const { lastInsertRowid: oid } = run(sb, "INSERT INTO orders (email, order_date, total_price) VALUES (?,?,?)", u.email, iso(), total);
        for (const it of cart) run(sb, "INSERT INTO order_items (orderID, product_name, quantity) VALUES (?,?,?)", oid, it.name, it.quantity);
        run(sb, "DELETE FROM cart WHERE email = ?", u.email);
        if (sb.visitorEmail) enqueueJob(sb, "order_confirmation");
        return send(res, 200, { success: "Checkout successful", order_id: Number(oid), totalPrice: total,
          cart_items: cart.map((c) => ({ productID: c.productID, name: c.name, price: c.price, quantity: c.quantity })) });
      }

      // ---- manager / admin ----
      case "GET /api/manager/dashboard": {
        if (!needStaff()) return;
        const cats = rows(sb, "SELECT categoryID, category_name FROM categories").map((c) => ({
          categoryID: c.categoryID, category_name: c.category_name,
          products: rows(sb, `SELECT p.productID, p.name AS product_name, p.price, p.unit, i.quantity, p.manufacture_date, p.expiry_date
            FROM products p LEFT JOIN inventory i ON i.productID = p.productID WHERE p.categoryID = ?`, c.categoryID),
        }));
        return send(res, 200, { categories: cats });
      }
      case "POST /api/manager/add_product": {
        if (!needStaff()) return;
        const cat = one(sb, "SELECT categoryID FROM categories WHERE category_name = ?", body.category_name);
        if (!cat || !body.product_name) return fail(res, 400, "Valid product name and category are required");
        const { lastInsertRowid: pid } = run(sb,
          "INSERT INTO products (name, manufacture_date, expiry_date, price, dynamic_price, is_recommended, categoryID, unit) VALUES (?,?,?,?,?,?,?,?)",
          body.product_name, body.manufacture_date || iso(), body.expiry_date || iso(30), Number(body.price) || 0, Number(body.price) || 0, 0, cat.categoryID, body.unit || "piece");
        run(sb, "INSERT INTO inventory (productID, quantity) VALUES (?, ?)", pid, Number(body.quantity) || 0);
        return send(res, 201, { success: "Product added to the category." });
      }
      case "POST /api/manager/edit_product": {
        if (!needStaff()) return;
        const cat = one(sb, "SELECT categoryID FROM categories WHERE category_name = ?", body.category_name);
        run(sb, "UPDATE products SET name=?, categoryID=?, price=?, dynamic_price=?, unit=?, manufacture_date=?, expiry_date=? WHERE productID=?",
          body.product_name, cat?.categoryID ?? 1, Number(body.price), Number(body.price), body.unit, body.manufacture_date, body.expiry_date, body.product_id);
        run(sb, "UPDATE inventory SET quantity = ? WHERE productID = ?", Number(body.quantity), body.product_id);
        return send(res, 200, { success: "Product updated." });
      }
      case "POST /api/manager/delete_product":
        if (!needStaff()) return;
        run(sb, "DELETE FROM inventory WHERE productID = ?", body.product_id);
        run(sb, "DELETE FROM products WHERE productID = ?", body.product_id);
        return send(res, 200, { success: "Product deleted." });
      case "POST /api/manager/categories/add_category":
        if (!needAdmin()) return;
        run(sb, "INSERT INTO categories (category_name) VALUES (?)", String(body.category_name || "").trim());
        return send(res, 201, { success: "Category added successfully" });
      case "POST /api/manager/categories/delete_category":
        if (!needAdmin()) return;
        run(sb, "DELETE FROM inventory WHERE productID IN (SELECT productID FROM products WHERE categoryID = ?)", body.categoryID);
        run(sb, "DELETE FROM products WHERE categoryID = ?", body.categoryID);
        run(sb, "DELETE FROM categories WHERE categoryID = ?", body.categoryID);
        return send(res, 200, { success: "Category deleted successfully" });
      case "GET /api/manager/prepare_report": {
        if (!needStaff()) return;
        const data = rows(sb, `SELECT oi.product_name n, SUM(oi.quantity) q, SUM(oi.quantity * p.price) t
          FROM order_items oi JOIN products p ON p.name = oi.product_name GROUP BY oi.product_name`);
        res.statusCode = 200;
        res.setHeader("Content-Type", "text/csv");
        res.setHeader("Content-Disposition", "attachment; filename=order_report.csv");
        res.end(["Product name,Quantity sold,Total amount", ...data.map((r) => `${r.n},${r.q},${r.t}`)].join("\n") + "\n");
        return;
      }
      case "GET /api/admin/manager_requests": {
        if (!needAdmin()) return;
        return send(res, 200, { manager_requests: rows(sb,
          `SELECT u.email, u.name, u.address, u.phone AS phone_no FROM store_manager s JOIN user_profiles u ON u.email = s.email WHERE s.manager_status = 'pending'`) });
      }
      case "POST /api/admin/accept_manager":
      case "POST /api/admin/reject_manager": {
        if (!needAdmin()) return;
        run(sb, "UPDATE store_manager SET manager_status = ? WHERE email = ?", path.endsWith("accept_manager") ? "approved" : "denied", body.email);
        return send(res, 200, { success: "Done." });
      }
      default:
        return fail(res, 404, `Mock: no handler for ${m} ${path}`);
    }
  }

  function productRows(sb, where) {
    return rows(sb, `SELECT p.productID, p.name, p.manufacture_date, p.expiry_date, p.price, p.dynamic_price, p.is_recommended,
        p.categoryID, p.unit, i.inventoryId, COALESCE(i.quantity, 0) AS stock
      FROM products p LEFT JOIN inventory i ON i.productID = p.productID WHERE ${where}`)
      .map((r) => [r.productID, r.name, r.manufacture_date, r.expiry_date, r.price, r.dynamic_price, r.is_recommended,
        r.categoryID, r.unit, r.inventoryId, r.productID, r.stock]);
  }

  return {
    name: "grocery-demo-mock",
    apply: "serve",
    configureServer(server) {
      console.log("[demo-mock] serving a fake demo API (VITE_DEMO_MOCK=true). Not part of the production build.");
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url, "http://localhost");
        if (!url.pathname.startsWith("/api/") && !url.pathname.startsWith("/__mock/")) return next();
        let raw = "";
        for await (const chunk of req) raw += chunk;
        let body = {};
        try { body = raw ? JSON.parse(raw) : {}; } catch { return send(res, 400, { error: "Invalid JSON" }); }
        try {
          await handle(req, res, url, body);
        } catch (error) {
          console.error("[demo-mock]", error);
          if (!res.writableEnded) send(res, 500, { error: String(error.message || error) });
        }
      });
    },
  };
}
