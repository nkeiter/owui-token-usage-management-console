"use strict";
// All dynamic text goes through textContent (via h()) - never innerHTML.

const MAX_BULK_USERS = 1000;
const EMAIL_RE = /^[^@\s,;<>"']{1,64}@[^@\s,;<>"']{1,255}\.[^@\s,;<>"']{2,63}$/;
const $ = (s) => document.querySelector(s);
const fmt = (n) => (n === null || n === undefined ? "–" : n === -1 ? "∞" : Number(n).toLocaleString());
const short = (n) => (n === -1 ? "∞" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n));

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : String(kid ?? ""));
  return el;
}

let cfg, token, charts = [];
const tierPrefix = () => cfg?.tierPrefix || "token-limit-";
const tierLabel = (name) => name.startsWith(tierPrefix()) ? name.slice(tierPrefix().length) : name;
const tierSuffix = (name) => {
  const normalized = name.trim().toLowerCase();
  return normalized.startsWith(tierPrefix()) ? normalized.slice(tierPrefix().length) : normalized;
};
const builtinTierNames = () => cfg?.builtinTiers || [
  "token-limit-enterprise", "token-limit-pro", "token-limit-standard", "token-limit-test",
];

// Per-tab view state (filters, sort order, report inputs). Survives re-renders after an
// action (route()) and page refreshes; cleared with the rest of sessionStorage on sign-out.
let viewState = {};
try { viewState = JSON.parse(sessionStorage.getItem("viewState") || "{}"); } catch (e) { viewState = {}; }
function tabState(tab) {
  if (!viewState[tab]) viewState[tab] = {};
  return viewState[tab];
}
function saveState() {
  try { sessionStorage.setItem("viewState", JSON.stringify(viewState)); } catch (e) { /* ignore */ }
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  setTimeout(() => (t.className = ""), 4000);
}

// ---------------------------------------------------------------- auth (Authorization Code + PKCE)
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const redirectUri = () => location.origin + "/";

async function login() {
  sessionStorage.setItem("returnHash", location.hash);
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  sessionStorage.setItem("pkce", JSON.stringify({ verifier, state }));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const q = new URLSearchParams({
    response_type: "code", client_id: cfg.clientId, redirect_uri: redirectUri(),
    scope: "openid email", code_challenge_method: "S256", code_challenge: challenge, state,
  });
  location.assign(`${cfg.cognitoDomain}/oauth2/authorize?${q}`);
}

async function finishLogin(code, state) {
  const saved = JSON.parse(sessionStorage.getItem("pkce") || "{}");
  sessionStorage.removeItem("pkce");
  history.replaceState({}, "", "/" + (sessionStorage.getItem("returnHash") || ""));
  sessionStorage.removeItem("returnHash");
  if (!saved.verifier || saved.state !== state) throw new Error("Login state mismatch");
  const r = await fetch(`${cfg.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: cfg.clientId, code,
      redirect_uri: redirectUri(), code_verifier: saved.verifier,
    }),
  });
  if (!r.ok) throw new Error("Token exchange failed");
  const t = await r.json();
  sessionStorage.setItem("session", JSON.stringify({ token: t.access_token, exp: Date.now() + (t.expires_in - 30) * 1000 }));
}

function loadSession() {
  try {
    const s = JSON.parse(sessionStorage.getItem("session") || "null");
    if (s && s.exp > Date.now()) return s.token;
  } catch (e) { /* ignore */ }
  sessionStorage.removeItem("session");
  return null;
}

function logout() {
  sessionStorage.clear();
  const q = new URLSearchParams({ client_id: cfg.clientId, logout_uri: redirectUri() });
  location.assign(`${cfg.cognitoDomain}/logout?${q}`);
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { sessionStorage.removeItem("session"); return login(); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

// ---------------------------------------------------------------- shared UI
function pctBar(pct) {
  if (pct === null) return h("span", { class: "muted" }, "unlimited");
  const cls = pct >= 100 ? "bad" : pct >= 80 ? "warn" : "";
  return h("span", {}, h("span", { class: `bar ${cls}` }, h("i", { style: `width:${Math.min(pct, 100)}%` })), `${pct}%`);
}

function sortableTable(cols, rows, id = "main") {
  const saved = (tabState(current).sort || {})[id] || {};
  let sortKey = Number.isInteger(saved.key) && saved.key < cols.length ? saved.key : null;
  let asc = saved.asc !== false;
  const table = h("table");
  const remember = () => {
    const st = tabState(current);
    st.sort = { ...(st.sort || {}), [id]: { key: sortKey, asc } };
    saveState();
  };
  const render = () => {
    const data = [...rows];
    if (sortKey !== null) {
      const c = cols[sortKey];
      const val = (r) => (c.sort ? c.sort(r) : c.text(r));
      data.sort((a, b) => {
        const x = val(a), y = val(b);
        return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * (asc ? 1 : -1);
      });
    }
    table.replaceChildren(
      h("thead", {}, h("tr", {}, cols.map((c, i) =>
        h("th", { class: c.num ? "num" : "", onclick: () => { asc = sortKey === i ? !asc : true; sortKey = i; remember(); render(); } },
          c.label + (sortKey === i ? (asc ? " ▲" : " ▼") : ""))))),
      h("tbody", {}, data.map((r) => h("tr", {}, cols.map((c) =>
        h("td", { class: c.num ? "num" : "" }, c.render ? c.render(r) : c.text(r)))))),
    );
  };
  render();
  return table;
}

function chart(canvas, config) {
  const c = new Chart(canvas, config);
  charts.push(c);
  return c;
}

function chartCard(title, build) {
  const canvas = h("canvas");
  const card = h("div", { class: "card" }, h("strong", {}, title), h("div", { class: "chartbox" }, canvas));
  queueMicrotask(() => build(canvas));
  return card;
}

// One default tier (select) plus any number of custom tiers (checkboxes); custom limits add up.
function tiersDialog(u, TIERS, after) {
  const dlg = h("dialog");
  const builtins = builtinTierNames();
  const current = u.default_tier || cfg.defaultTier;
  const sel = h("select", {}, builtins.map((o) => h("option", { value: o, selected: o === current }, o)));
  const customs = TIERS.filter((t) => !builtins.includes(t));
  const have = new Set(u.custom_tiers || []);
  const boxes = customs.map((t) => ({ t, box: h("input", { type: "checkbox", checked: have.has(t) }) }));
  const err = h("p", { class: "error" });
  const form = h("form", { method: "dialog" }, h("h3", {}, `Tiers – ${u.name || u.email}`),
    h("label", {}, "Default tier (one)"), sel,
    h("p", { class: "muted" }, "Custom tiers (any number). Their limits are added together and replace the default tier's limits."),
    ...(boxes.length ? boxes.map(({ t, box }) => h("label", { class: "check" }, box, ` ${tierLabel(t)}`))
      : [h("p", { class: "muted" }, "No custom tiers exist yet.")]),
    err,
    h("div", { class: "actions" }, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"),
      h("button", { type: "submit", class: "primary" }, "Save")));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      // Also send it when the user has no default group yet, or several, so Standard can be set explicitly.
      if (sel.value !== u.default_tier || u.tier_memberships !== 1) await api("PUT", `/api/users/${u.id}/tier`, { tier: sel.value });
      for (const { t, box } of boxes) {
        if (box.checked && !have.has(t)) await api("PUT", `/api/users/${u.id}/tier`, { tier: t });
        if (!box.checked && have.has(t)) await api("DELETE", `/api/users/${u.id}/tier?tier=${encodeURIComponent(t)}`);
      }
      dlg.close(); toast("Tiers updated"); after();
    } catch (x) { err.textContent = x.message; }
  });
  dlg.append(form); document.body.append(dlg); dlg.addEventListener("close", () => dlg.remove()); dlg.showModal();
}

function dialog(title, fields, onSave, saveLabel = "Save") {
  const dlg = h("dialog");
  const inputs = {};
  const form = h("form", { method: "dialog" }, h("h3", {}, title));
  for (const f of fields) {
    const input = f.options
      ? h("select", {}, f.options.map((o) => h("option", { value: o, selected: o === f.value }, o)))
      : h("input", { type: f.type || "text", value: f.value ?? "", placeholder: f.placeholder || "" });
    inputs[f.key] = input;
    form.append(h("label", {}, f.label), input);
  }
  form.append(h("div", { class: "actions" },
    h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"),
    h("button", { type: "submit", class: "primary" }, saveLabel)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await onSave(Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value])));
      dlg.close();
    } catch (err) { toast(err.message, true); }
  });
  dlg.append(form);
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}

// ---------------------------------------------------------------- views
async function viewDashboard(main) {
  const [o, rep] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/report")]);
  const card = (label, value, warn) => h("div", { class: "card" }, h("div", { class: "muted" }, label), h("div", { class: "n", style: warn ? "color:var(--bad)" : "" }, value));
  main.append(
    h("div", { class: "cards" },
      card("Tokens today", fmt(o.tokens_today)), card("Tokens this month", fmt(o.tokens_month)),
      card("Users", fmt(o.total_users)), card("Over a limit", fmt(o.users_over_limit), o.users_over_limit > 0),
      card("Near limit (≥80%)", fmt(o.users_near_limit)), card("Users in several tiers", fmt(o.ambiguous_users), o.ambiguous_users > 0)),
    h("div", { class: "grid2" },
      chartCard("Daily tokens – last 30 days", (cv) => chart(cv, {
        type: "line",
        data: { labels: rep.daily.map((d) => d.date.slice(5)), datasets: [
          { label: "Prompt", data: rep.daily.map((d) => d.prompt), borderColor: "#5aa9ff", fill: true, backgroundColor: "#5aa9ff33", tension: .25 },
          { label: "Completion", data: rep.daily.map((d) => d.completion), borderColor: "#e8a200", fill: true, backgroundColor: "#e8a20033", tension: .25 }] },
        options: { maintainAspectRatio: false, scales: { y: { stacked: true, beginAtZero: true } } },
      })),
      chartCard("Month-to-date tokens by tier", (cv) => chart(cv, {
        type: "bar",
        data: { labels: o.tiers.map((t) => tierLabel(t.name)), datasets: [{ label: "Tokens", data: o.tiers.map((t) => t.tokens_month), backgroundColor: "#5aa9ff" }] },
        options: { maintainAspectRatio: false, plugins: { legend: { display: false } } },
      }))),
    h("h3", {}, "Closest to their limit"),
    sortableTable([
      { label: "User", text: (u) => u.name || u.email },
      { label: "Tier", text: (u) => u.tiers.map(tierLabel).join(", ") },
      { label: "Today", render: (u) => pctBar(u.daily_pct), text: (u) => u.daily_pct ?? -1, sort: (u) => u.daily_pct ?? -1 },
      { label: "Month", render: (u) => pctBar(u.monthly_pct), text: (u) => u.monthly_pct ?? -1, sort: (u) => u.monthly_pct ?? -1 },
    ], o.near_limit),
  );
}

const tierNames = async () => (await api("GET", "/api/tiers")).tiers.map((t) => t.name);

// Tolerant CSV reader: BOM, CRLF, quotes, and the delimiter (comma, semicolon, tab or pipe) is sniffed
// from the first lines. Returns an array of rows (arrays of trimmed cells).
function readCsvRows(text) {
  text = text.replace(/^\uFEFF/, "");
  const sample = text.split(/\r?\n/).slice(0, 5).join("\n").replace(/"[^"]*"/g, "");
  const delim = [",", ";", "\t", "|"].map((d) => [d, sample.split(d).length]).sort((x, y) => y[1] - x[1])[0][0];
  const rows = [], lines = [];
  let row = [], cell = "", quoted = false, line = 1, start = 1;
  const endCell = () => { row.push(cell.trim()); cell = ""; };
  const endRow = () => { endCell(); if (row.some((c) => c)) { rows.push(row); lines.push(start); } row = []; start = line; };
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === "\n") line++;
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === delim) endCell();
    else if (c === "\n") endRow();
    else if (c !== "\r") cell += c;
  }
  endRow();
  rows.lines = lines;
  return rows;
}

// Any-format CSV: the email column is detected automatically (the column with the most valid
// addresses; a header containing "mail" breaks ties) and every other column is ignored. A name
// column is used when the header has one.
function parseCsv(text) {
  const rows = readCsvRows(text);
  const find = (c) => { const m = (c || "").match(/[^\s<>,;"']+@[^\s<>,;"']+\.[^\s<>,;"']+/); return m && EMAIL_RE.test(m[0]) ? m[0].toLowerCase() : ""; };
  const width = Math.max(0, ...rows.map((r) => r.length));
  let ec = -1, best = 0;
  for (let c = 0; c < width; c++) {
    const n = rows.filter((r) => find(r[c])).length;
    const mailHeader = /e-?mail/i.test(rows[0]?.[c] || "");
    if (n > best || (n === best && n > 0 && mailHeader && !/e-?mail/i.test(rows[0]?.[ec] || ""))) { best = n; ec = c; }
  }
  if (ec < 0) return { people: [], invalid: [], noEmailColumn: rows.length > 0 };
  const hasHeader = !(rows[0][ec] || "").includes("@");
  const header = hasHeader ? rows[0].map((c) => c.toLowerCase()) : [];
  const nc = header.findIndex((c) => ["name", "full name", "display name", "fullname"].includes(c));
  const people = [], invalid = [];
  for (const r of rows.slice(hasHeader ? 1 : 0)) {
    const email = find(r[ec]);
    if (email) people.push({ email, name: nc >= 0 ? (r[nc] || "") : "" });
    else invalid.push(r[ec] || r.join(" "));
  }
  return { people, invalid, noEmailColumn: false };
}

// Search-or-upload picker that adds the selection to one tier (creates unknown users).
async function addUsersDialog(tier) {
  const { users } = await api("GET", "/api/users");
  const byEmail = new Map(users.map((u) => [(u.email || "").toLowerCase(), u]));
  const selected = new Map();
  const select = (u, extra = {}) => {
    const key = u.id || u.email.toLowerCase();
    if (selected.size >= MAX_BULK_USERS && !selected.has(key)) { toast(`At most ${MAX_BULK_USERS} users at once`, true); return false; }
    selected.set(key, { id: u.id, email: u.email, name: u.name || extra.name || "", isNew: !u.id });
    return true;
  };
  const dlg = h("dialog", { class: "wide" });
  const search = h("input", { placeholder: "Search users by name or email, or type a new email…", autocomplete: "off" });
  const results = h("div", { class: "picklist" });
  const chosen = h("div", { class: "picklist chosen" });
  const summary = h("div", { class: "muted" });
  const file = h("input", { type: "file", accept: ".csv,.txt,text/csv,text/plain" });
  const go = h("button", { type: "submit", class: "primary" }, "Add Users");

  const drawChosen = () => {
    const items = [...selected.entries()];
    chosen.replaceChildren(...items.map(([key, u]) => h("div", { class: "pick" },
      h("span", {}, u.name ? `${u.name} <${u.email}>` : u.email,
        u.isNew ? h("span", { class: "tag warn" }, " new – will be created") : "",
        !u.isNew && users.find((x) => x.id === u.id)?.tiers.includes(tier) ? h("span", { class: "tag" }, " already in tier") : ""),
      h("button", { type: "button", title: "Remove", onclick: () => { selected.delete(key); drawChosen(); drawResults(); } }, "×"))));
    if (!items.length) chosen.replaceChildren(h("div", { class: "muted" }, "Nobody selected yet."));
    const fresh = items.filter(([, u]) => u.isNew).length;
    summary.textContent = `${items.length} selected` + (fresh ? `, ${fresh} will be created as new users` : "") + ". A custom tier is added to the user's existing tiers; a built-in tier replaces their other built-in tier.";
    go.disabled = !items.length;
  };
  const drawResults = () => {
    const q = search.value.trim().toLowerCase();
    if (!q) return results.replaceChildren();
    const rows = users.filter((u) => !selected.has(u.id) && `${u.name} ${u.email}`.toLowerCase().includes(q)).slice(0, 8)
      .map((u) => h("div", { class: "pick" }, h("span", {}, `${u.name || u.email} `, h("span", { class: "muted" }, u.name ? u.email : ""),
        h("span", { class: "tag" }, ` ${u.tiers.map(tierLabel).join(", ")}`)),
      h("button", { type: "button", onclick: () => { if (select(u)) { search.value = ""; drawChosen(); drawResults(); } } }, "Add")));
    if (EMAIL_RE.test(q) && !byEmail.has(q) && !selected.has(q)) {
      rows.push(h("div", { class: "pick" }, h("span", {}, `${q} `, h("span", { class: "tag warn" }, "new user")),
        h("button", { type: "button", onclick: () => { if (select({ email: q })) { search.value = ""; drawChosen(); drawResults(); } } }, "Add as new")));
    }
    results.replaceChildren(...(rows.length ? rows : [h("div", { class: "muted" }, "No matches.")]));
  };
  search.addEventListener("input", drawResults);
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });

  file.addEventListener("change", async () => {
    const f = file.files[0];
    if (!f) return;
    if (f.size > 300000) { toast("CSV file too large (max 300 KB)", true); file.value = ""; return; }
    const { people, invalid, noEmailColumn } = parseCsv(await f.text());
    if (noEmailColumn) { file.value = ""; toast("No column with email addresses was found in the file", true); return; }
    let added = 0;
    for (const p of people) {
      const existing = byEmail.get(p.email);
      if (selected.has(existing ? existing.id : p.email)) continue;
      if (!select(existing || p, p)) break;
      added++;
    }
    file.value = "";
    drawChosen();
    toast(`${added} user(s) loaded from CSV` + (invalid.length ? `, ${invalid.length} invalid row(s) skipped` : ""), !added && invalid.length > 0);
  });

  const form = h("form", { method: "dialog" },
    h("h3", {}, `Add users – ${tierLabel(tier)}`),
    h("p", { class: "muted" }, "Search for existing users, type a new email address, or upload a CSV in any layout (the column holding email addresses is detected automatically and the other columns are ignored). The selection below is added when you click Add Users. Unknown e-mails get an Open WebUI account (role: user) that links to their SSO sign-in."),
    search, results, h("div", { class: "csvrow" }, h("label", {}, "Or upload a CSV"), file),
    h("h4", {}, "Selected users"), chosen, summary,
    h("div", { class: "actions" }, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"), go));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    go.disabled = true;
    try {
      const items = [...selected.values()];
      const r = await api("POST", `/api/tiers/${tier}/members`, {
        user_ids: items.filter((u) => u.id).map((u) => u.id),
        users: items.filter((u) => !u.id).map((u) => ({ email: u.email, name: u.name })),
      });
      toast(`${r.added} added` + (r.created ? `, ${r.created} created` : "") + (r.moved_from_other_tiers ? `, ${r.moved_from_other_tiers} switched default tier` : "") + (r.already_in_tier ? `, ${r.already_in_tier} already in tier` : ""));
      dlg.close();
      route();
    } catch (err) { toast(err.message, true); go.disabled = false; }
  });
  dlg.append(form);
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  drawChosen();
  dlg.showModal();
  search.focus();
}

const IMPORT_COLUMNS = {
  group: ["groupname", "group", "tier", "tiername"],
  email: ["email", "emailaddress", "mail"],
  daily: ["dailytokenlimit", "dailylimit", "daily"],
  monthly: ["monthlytokenlimit", "monthlylimit", "monthly"],
  description: ["description", "desc"],
};
const IMPORT_TEMPLATE = "group-name,email,daily-token-limit,monthly-token-limit,description\n" +
  "econ-104-fall-2026-a,student1@example.edu,500K,5M,Econ 104 section A\n" +
  "econ-104-fall-2026-a,student2@example.edu,,,\n" +
  "bio-200-fall-2026,student3@example.edu,1000000,10000000,\n" +
  "bio-200-fall-2026,student4@example.edu,,,\n";

// Turn the uploaded CSV text into {line, group, email, daily, monthly, description} rows.
function importRowsFromCsv(text) {
  const all = readCsvRows(text);
  if (!all.length) return { error: "The file is empty." };
  const norm = (c) => c.toLowerCase().replace(/[^a-z0-9]/g, "");
  const idx = {};
  for (const [key, names] of Object.entries(IMPORT_COLUMNS)) idx[key] = all[0].findIndex((c) => names.includes(norm(c)));
  // With no recognisable column names the first row is data and the columns are positional:
  // group-name, email, daily-token-limit, monthly-token-limit, description.
  const hasHeader = Object.values(idx).some((i) => i >= 0);
  if (!hasHeader) {
    if (!EMAIL_RE.test(all[0][1] || "")) {
      return { error: "No header row was found, and the second column of the first row is not an email address. Use the column order group-name,email,daily-token-limit,monthly-token-limit,description (a header row is optional)." };
    }
    Object.keys(IMPORT_COLUMNS).forEach((k, i) => { idx[k] = i; });
  }
  const missing = ["group", "email"].filter((k) => idx[k] < 0);
  if (missing.length) {
    return { error: `The header row must include the columns group-name and email (missing: ${missing.map((k) => k === "group" ? "group-name" : k).join(", ")}). ` +
      `Found: ${all[0].join(", ") || "(nothing)"}` };
  }
  const get = (r, k) => (idx[k] >= 0 ? r[idx[k]] || "" : "");
  const first = hasHeader ? 1 : 0;
  const rows = all.slice(first).map((r, i) => ({
    line: all.lines[i + first], group: get(r, "group"), email: get(r, "email"),
    daily: get(r, "daily"), monthly: get(r, "monthly"), description: get(r, "description"),
  }));
  if (!rows.length) return { error: "The file has a header but no data rows." };
  return { rows };
}

// Upload a CSV describing several tiers and their members, preview the effect, then create them.
function importTiersDialog() {
  const dlg = h("dialog", { class: "wide" });
  const file = h("input", { type: "file", accept: ".csv,.txt,text/csv,text/plain" });
  const out = h("div");
  const go = h("button", { type: "button", class: "primary", disabled: true }, "Create groups");
  let rows = null; // parsed rows; dropped as soon as the dialog closes or the import is committed
  const wipe = () => { rows = null; file.value = ""; };

  const lines = (items, cls) => items.length ? h("ul", { class: cls }, items.map((t) => h("li", {}, t))) : "";
  const render = (plan, error) => {
    go.disabled = true;
    if (error) return out.replaceChildren(h("div", { class: "grp bad" }, h("strong", {}, "Cannot read this file"), h("p", {}, error)));
    const bad = plan.groups.filter((g) => g.status === "error").length;
    const members = plan.groups.reduce((n, g) => n + g.members, 0);
    go.disabled = !plan.can_commit;
    out.replaceChildren(
      h("div", { class: "grp " + (plan.can_commit ? "ok" : "bad") },
        h("strong", {}, plan.can_commit
          ? `Ready: ${plan.groups.length} group(s) will be created with ${members} membership(s)` + (plan.new_users ? `; ${plan.new_users} new user(s) will be created.` : ".")
          : `Not ready: ${bad} of ${plan.groups.length} group(s) have errors` + (plan.errors.length ? ", and some rows have no group name" : "") + ". Nothing has been created. Fix the CSV and upload it again.")),
      plan.errors.length ? h("div", { class: "grp bad" }, h("strong", {}, "File errors"), lines(plan.errors, "errs")) : "",
      ...plan.groups.map((g) => h("div", { class: "grp " + (g.status === "error" ? "bad" : "ok") },
        h("div", {}, h("span", { class: "tag " + (g.status === "error" ? "bad" : "good") }, g.status === "error" ? "Has errors" : "Will be created"), " ",
          h("strong", {}, g.name), g.input_name !== g.name ? h("span", { class: "muted" }, `  (from "${g.input_name}")`) : ""),
        h("div", { class: "muted" },
          `Limits ${fmt(g.daily)}/day, ${fmt(g.monthly)}/month` + (g.daily === -1 || g.monthly === -1 ? " (-1 = unlimited)" : "") +
          ` · ${g.members} member(s)` + (g.new_users ? `, ${g.new_users} new` : "") + (g.existing_users ? `, ${g.existing_users} existing` : "") +
          ` · description: "${g.description || ""}"` + (g.description_defaulted ? " (default)" : "")),
        lines(g.errors, "errs"), lines(g.warnings, "warns"))));
  };
  const load = async () => {
    const f = file.files[0];
    if (!f) return;
    if (f.size > 1000000) { wipe(); return render(null, "The file is larger than 1 MB."); }
    const parsed = importRowsFromCsv(await f.text());
    file.value = ""; // the browser no longer holds a reference to the file once it has been read
    if (parsed.error) { rows = null; return render(null, parsed.error); }
    rows = parsed.rows;
    try { render(await api("POST", "/api/tiers/import", { rows, commit: false })); }
    catch (err) { rows = null; render(null, err.message); }
  };
  file.addEventListener("change", load);
  go.addEventListener("click", async () => {
    if (!rows) return;
    go.disabled = true;
    try {
      const plan = await api("POST", "/api/tiers/import", { rows, commit: true });
      toast(`${plan.groups.length} group(s) created` + (plan.new_users ? `, ${plan.new_users} new user(s)` : ""));
      wipe(); dlg.close(); route();
    } catch (err) { toast(err.message, true); go.disabled = false; }
  });

  const template = h("a", { href: "#", onclick: (e) => {
    e.preventDefault();
    const a = h("a", { href: URL.createObjectURL(new Blob([IMPORT_TEMPLATE], { type: "text/csv" })), download: "tier-import-template.csv" });
    document.body.append(a); a.click(); a.remove();
  } }, "Download an example CSV");
  dlg.append(h("form", { method: "dialog" },
    h("h3", {}, "Add multiple groups from a CSV"),
    h("p", { class: "muted" }, "One row per member. The header row is optional; the columns are, in this order (a header may list them in any order):"),
    h("pre", { class: "fmt" }, "group-name,email,daily-token-limit,monthly-token-limit,description"),
    h("ul", { class: "muted" },
      h("li", {}, h("strong", {}, "group-name"), ` (required): rows are grouped by name wherever they appear in the file. ${tierPrefix()} is added unless already present.`),
      h("li", {}, h("strong", {}, "email"), " (required on every row): unknown emails get an Open WebUI account. Users keep their other tiers (several custom tiers add up)."),
      h("li", {}, h("strong", {}, "daily-token-limit / monthly-token-limit"), " (needed on at least one row per group): the greatest value in the group is used. Plain numbers, 500K or 5M; -1 = unlimited."),
      h("li", {}, h("strong", {}, "description"), " (optional): the first non-empty value in the group is used; otherwise it is the limits, e.g. \"500K/day, 5M/month\".")),
    h("p", { class: "muted" }, "A group whose name already exists is rejected. You will see a summary of what would happen before anything is created. The file is read in your browser and is never stored; only its parsed contents are sent for the preview and the final import. ", template),
    h("div", { class: "csvrow" }, h("label", {}, "CSV file"), file),
    out,
    h("div", { class: "actions" }, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"), go)));
  dlg.addEventListener("close", () => { wipe(); dlg.remove(); });
  document.body.append(dlg);
  dlg.showModal();
}

function createTierDialog() {
  dialog("New custom tier", [
    { key: "name", label: `Name (stored as ${tierPrefix()}<name>; letters, numbers, hyphens)`, placeholder: "e.g. spring-2026" },
    { key: "daily", label: "Daily tokens (-1 = unlimited)", type: "number", value: cfg.defaultDailyTokens },
    { key: "monthly", label: "Monthly tokens (-1 = unlimited)", type: "number", value: cfg.defaultMonthlyTokens },
    { key: "description", label: "Description (optional; defaults to the limits, e.g. 500K/day, 5M/month)" }],
  async (v) => {
    const r = await api("POST", "/api/tiers", v);
    toast(`Tier ${r.name} created`);
    await route();
    addUsersDialog(r.name).catch((e) => toast(e.message, true));
  }, "Create tier");
}

async function viewUsers(main) {
  const [{ users }, TIERS] = await Promise.all([api("GET", "/api/users"), tierNames()]);
  const st = tabState("users");
  const filter = h("input", {
    placeholder: "Filter by name, email or tier…", value: st.filter || "",
    oninput: () => { st.filter = filter.value; saveState(); draw(); },
  });
  const holder = h("div");
  const edit = {
    tier: (u) => tiersDialog(u, TIERS, route),
    limit: (u) => dialog(`Limit override – ${u.name || u.email}`, [
      { key: "daily", label: "Daily tokens (-1 = unlimited)", type: "number", value: u.daily_limit },
      { key: "monthly", label: "Monthly tokens (-1 = unlimited)", type: "number", value: u.monthly_limit },
      { key: "notes", label: "Notes (optional)" }],
      async (v) => { await api("PUT", `/api/users/${u.id}/override`, v); toast("Override saved"); route(); }),
    clear: async (u) => { if (confirm(`Remove override for ${u.name || u.email}?`)) { await api("DELETE", `/api/users/${u.id}/override`); toast("Override removed"); route(); } },
  };
  const count = h("span", { class: "muted" });
  const chip = h("span");
  const reportBtn = h("button", { title: "Open Reports for the users currently shown" }, "Report");
  let shown = [];
  reportBtn.onclick = () => reportForUsers(shown, st.tier, !!filter.value.trim());
  const draw = () => {
    const q = filter.value.toLowerCase();
    const rows = shown = users.filter((u) => (!st.tier || u.tiers.includes(st.tier)) && `${u.name} ${u.email} ${u.tiers.join(" ")}`.toLowerCase().includes(q));
    reportBtn.disabled = !rows.length;
    count.textContent = rows.length === users.length ? `${users.length} users` : `${rows.length}/${users.length} users`;
    chip.replaceChildren(st.tier ? h("span", { class: "tag" }, `Tier: ${tierLabel(st.tier)} `,
      h("button", { type: "button", title: "Clear tier filter", onclick: () => { st.tier = ""; saveState(); draw(); } }, "×")) : "");
    holder.replaceChildren(sortableTable([
      { label: "User", text: (u) => u.name || u.email, render: (u) => h("span", {}, u.name || u.email, h("div", { class: "muted" }, u.name ? u.email : "")) },
      { label: "Tier", text: (u) => u.tiers.map(tierLabel).join(", "), render: (u) => h("span", {}, u.tiers.map(tierLabel).join(", "),
          u.tier_is_default ? h("span", { class: "tag" }, " default") : "", u.tier_memberships > 1 ? h("span", { class: "tag warn" }, " multiple") : "") },
      { label: "Today", num: true, text: (u) => u.tokens_today, render: (u) => fmt(u.tokens_today) },
      { label: "Daily limit", num: true, text: (u) => fmt(u.daily_limit), sort: (u) => u.daily_limit },
      { label: "Daily %", render: (u) => pctBar(u.daily_pct), text: (u) => u.daily_pct ?? -1, sort: (u) => u.daily_pct ?? -1 },
      { label: "Month", num: true, text: (u) => u.tokens_month, render: (u) => fmt(u.tokens_month) },
      { label: "Monthly limit", num: true, text: (u) => fmt(u.monthly_limit), sort: (u) => u.monthly_limit },
      { label: "Monthly %", render: (u) => pctBar(u.monthly_pct), text: (u) => u.monthly_pct ?? -1, sort: (u) => u.monthly_pct ?? -1 },
      { label: "Override", text: (u) => (u.has_override ? "yes" : ""), render: (u) => (u.has_override ? h("span", { class: "tag" }, "override") : "") },
      { label: "", render: (u) => h("span", {},
          h("button", { onclick: () => edit.tier(u) }, "Tier"), " ", h("button", { onclick: () => edit.limit(u) }, "Limit"), " ",
          h("button", { onclick: () => reportForUsers([u], "", true) }, "Report"),
          u.has_override ? [" ", h("button", { class: "danger", onclick: () => edit.clear(u) }, "Clear")] : "") },
    ].map((c) => ({ ...c, text: c.text || (() => "") })), rows));
  };
  main.append(
    h("div", { class: "notice" },
      `User tier membership is managed through native Open WebUI groups (${tierPrefix()}* groups). ` +
      "The Tier button below is a convenient way to change a user's tier without manually editing the groups."),
    h("div", { class: "toolbar" }, filter, chip, count, reportBtn), holder);
  draw();
}

// Open Reports for the given users. A whole, unsearched tier uses the tier filter (no user cap).
function reportForUsers(rows, tier, searched) {
  const st = tabState("reports");
  st.user = ""; st.tier = ""; st.userIds = []; st.userLabel = "";
  if (tier && !searched) st.tier = tier;
  else if (rows.length === 1) st.user = rows[0].id;
  else if (rows.length > 200) { toast("Too many users for one report (max 200). Narrow the filter or filter by tier.", true); return; }
  else { st.userIds = rows.map((u) => u.id); st.userLabel = `${rows.length} selected users`; }
  saveState();
  location.hash = "#" + TAB_TO_HASH.reports;
}

// Jump to the Users tab filtered to exactly one tier.
function viewTierUsers(tier) {
  const st = tabState("users");
  st.tier = tier;
  st.filter = "";
  saveState();
  location.hash = "#" + TAB_TO_HASH.users;
}

// Open the Reports tab filtered to one tier.
function viewTierReport(tier) {
  const st = tabState("reports");
  st.tier = tier;
  saveState();
  location.hash = "#" + TAB_TO_HASH.reports;
}

async function viewTiers(main) {
  const { tiers } = await api("GET", "/api/tiers");
  const del = async (t) => {
    const msg = `Delete tier ${t.name}?\n\n` +
      `Its ${t.members} member(s) who are in no other tier will be returned to the ${cfg.defaultTier} tier. ` +
      "The Open WebUI group is deleted and the tier's limits are removed. This cannot be undone.";
    if (!confirm(msg)) return;
    try {
      const r = await api("DELETE", `/api/tiers/${t.name}`);
      toast(`Tier deleted; ${r.returned_to_standard} user(s) returned to ${tierLabel(cfg.defaultTier)}`);
      route();
    } catch (err) { toast(err.message, true); }
  };
  main.append(
    h("div", { class: "toolbar" },
      h("button", { class: "primary", onclick: createTierDialog }, "Add New"),
      h("button", { onclick: importTiersDialog }, "Add Multiple"),
      h("span", { class: "muted" }, `Tier membership is managed through native Open WebUI groups. Limits are stored here. Custom tiers are ${tierPrefix()}* groups.`)),
    sortableTable([
      { label: "Tier", text: (t) => t.name, render: (t) => h("span", {}, t.name, h("span", { class: "tag" }, t.custom ? " custom" : " default")) },
      { label: "Members", num: true, text: (t) => t.members, render: (t) => fmt(t.members) },
      { label: "Daily limit", num: true, text: (t) => fmt(t.daily_limit), sort: (t) => t.daily_limit ?? 0 },
      { label: "Monthly limit", num: true, text: (t) => fmt(t.monthly_limit), sort: (t) => t.monthly_limit ?? 0 },
      { label: "Description", text: (t) => t.description || "" },
      { label: "Updated", text: (t) => (t.updated_at ? new Date(t.updated_at).toLocaleString() : "") },
      { label: "", text: () => "", render: (t) => h("span", {},
          h("button", { onclick: () => dialog(`Edit ${t.name}`, [
            ...(t.custom ? [{ key: "name", label: `Name (after ${tierPrefix()})`, value: tierLabel(t.name) }] : []),
            { key: "daily", label: "Daily tokens (-1 = unlimited)", type: "number", value: t.daily_limit },
            { key: "monthly", label: "Monthly tokens (-1 = unlimited)", type: "number", value: t.monthly_limit },
            { key: "description", label: "Description", value: t.description || "" }],
          async (v) => { if (t.custom && tierSuffix(v.name) === tierLabel(t.name)) delete v.name; await api("PUT", `/api/tiers/${t.name}`, v); toast("Tier updated"); route(); }) }, "Edit"),
          " ", h("button", { onclick: () => addUsersDialog(t.name).catch((e) => toast(e.message, true)) }, "Add users"),
          " ", h("button", { onclick: () => viewTierUsers(t.name) }, "View Users"),
          " ", h("button", { onclick: () => viewTierReport(t.name) }, "Report"),
          t.custom ? [" ", h("button", { class: "danger", onclick: () => del(t) }, "Delete")] : "") },
    ], tiers));
}

async function viewReports(main) {
  const today = new Date().toISOString().slice(0, 10);
  const st = tabState("reports");
  const keep = (key) => () => { st[key] = { start, end, user, tier }[key].value; saveState(); };
  const start = h("input", { type: "date", value: st.start || new Date(Date.now() - 29 * 864e5).toISOString().slice(0, 10), onchange: () => keep("start")() });
  const end = h("input", { type: "date", value: st.end || today, onchange: () => keep("end")() });
  const user = h("input", { placeholder: "Optional user id", value: st.user || "", oninput: () => keep("user")() });
  const tiers = await tierNames();
  const tier = h("select", { onchange: () => keep("tier")() },
    h("option", { value: "" }, "All tiers"),
    ...tiers.map((t) => h("option", { value: t }, t)));
  tier.value = tiers.includes(st.tier) ? st.tier : "";
  const group = h("span");
  const drawGroup = () => group.replaceChildren(st.userIds && st.userIds.length ? h("span", { class: "tag" }, `${st.userLabel} `,
    h("button", { type: "button", title: "Clear user selection", onclick: () => { st.userIds = []; saveState(); drawGroup(); } }, "×")) : "");
  drawGroup();
  const out = h("div");
  const run = async () => {
    charts.forEach((c) => c.destroy()); charts = [];
    const q = new URLSearchParams({ start: start.value, end: end.value, ...(user.value.trim() ? { user_id: user.value.trim() } : {}), ...(tier.value ? { tier: tier.value } : {}), ...(st.userIds && st.userIds.length ? { user_ids: st.userIds.join(",") } : {}) });
    const r = await api("GET", `/api/report?${q}`);
    const csv = () => {
      const lines = ["date,prompt,completion,total,requests", ...r.daily.map((d) => `${d.date},${d.prompt},${d.completion},${d.total},${d.requests}`)];
      const a = h("a", { href: URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" })), download: `usage_${r.start}_${r.end}.csv` });
      document.body.append(a); a.click(); a.remove();
    };
    out.replaceChildren(
      h("div", { class: "cards" },
        h("div", { class: "card" }, h("div", { class: "muted" }, "Total tokens"), h("div", { class: "n" }, fmt(r.total_tokens))),
        h("div", { class: "card" }, h("div", { class: "muted" }, "Requests"), h("div", { class: "n" }, fmt(r.total_requests))),
        h("div", { class: "card" }, h("div", { class: "muted" }, "Avg / day"), h("div", { class: "n" }, fmt(Math.round(r.total_tokens / Math.max(r.daily.length, 1)))))),
      h("div", { class: "grid2" },
        chartCard("Tokens per day", (cv) => chart(cv, { type: "bar",
          data: { labels: r.daily.map((d) => d.date), datasets: [
            { label: "Prompt", data: r.daily.map((d) => d.prompt), backgroundColor: "#5aa9ff" },
            { label: "Completion", data: r.daily.map((d) => d.completion), backgroundColor: "#e8a200" }] },
          options: { maintainAspectRatio: false, scales: { x: { stacked: true }, y: { stacked: true } } } })),
        chartCard("Top users", (cv) => chart(cv, { type: "bar",
          data: { labels: r.top_users.map((u) => u.name), datasets: [{ label: "Tokens", data: r.top_users.map((u) => u.tokens), backgroundColor: "#5aa9ff" }] },
          options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false } } } })),
        chartCard("By tier (users' current tier)", (cv) => chart(cv, { type: "doughnut",
          data: { labels: r.by_tier.map((t) => tierLabel(t.tier)), datasets: [{ data: r.by_tier.map((t) => t.tokens) }] },
          options: { maintainAspectRatio: false } })),
        chartCard("By model", (cv) => chart(cv, { type: "doughnut",
          data: { labels: r.by_model.map((m) => m.model), datasets: [{ data: r.by_model.map((m) => m.tokens) }] },
          options: { maintainAspectRatio: false } }))),
      h("div", { class: "toolbar" }, h("button", { onclick: csv }, "Download daily CSV")),
      sortableTable([
        { label: "User", text: (u) => u.name }, { label: "Email", text: (u) => u.email },
        { label: "Tokens", num: true, text: (u) => u.tokens, render: (u) => fmt(u.tokens) },
        { label: "Requests", num: true, text: (u) => u.requests, render: (u) => fmt(u.requests) },
      ], r.top_users, "top-users"));
  };
  main.append(h("div", { class: "toolbar" }, "From", start, "to", end, tier, user, group,
    h("button", { class: "primary", onclick: () => run().catch((e) => toast(e.message, true)) }, "Run report")), out);
  await run();
}

async function viewAudit(main) {
  const [a, TIERS] = await Promise.all([api("GET", "/api/audit"), tierNames()]);
  const fix = (u) => tiersDialog(u, TIERS, route);
  const list = (id, rows, extra) => sortableTable([
    { label: "User", text: (u) => u.name || u.email }, { label: "Email", text: (u) => u.email },
    { label: extra.label, text: extra.text },
    { label: "", text: () => "", render: (u) => h("button", { onclick: () => fix(u) }, "Set tier") }], rows, id);
  main.append(
    h("h3", {}, `In several default tier groups (${a.multiple.length})`),
    h("p", { class: "muted" }, "A user has one default tier; the most recently added one is used. Saving a default tier removes the others."),
    list("multiple", a.multiple, { label: "Groups (newest first)", text: (u) => u.groups.join(" > ") }),
    h("h3", {}, `In no default tier group (${a.none.length})`),
    h("p", { class: "muted" }, `These users are treated as ${tierLabel(cfg.defaultTier)} until a default tier group is assigned (the usage filter also enrolls them in ${cfg.defaultTier} when it loads).`),
    noDefaultSection(a.none, builtinTierNames()));
}

// "No default tier" list with row checkboxes and a bulk "assign to default tier" action.
function noDefaultSection(rows, defaultTiers) {
  const selected = new Set();
  const boxes = () => [...table.querySelectorAll("input.pick")];
  const selectAll = h("button", {}, "Select All");
  const assign = h("button", { class: "primary" }, "Assign to Tier");
  const tierSel = h("select", {}, defaultTiers.map((t) => h("option", { value: t, selected: t === cfg.defaultTier }, tierLabel(t))));
  const refresh = () => {
    const all = rows.length > 0 && selected.size === rows.length;
    selectAll.textContent = all ? "Clear Selection" : "Select All";
    assign.textContent = selected.size ? `Assign ${selected.size} to Tier` : "Assign to Tier";
  };
  const setAll = (on) => {
    selected.clear();
    if (on) rows.forEach((u) => selected.add(u.id));
    boxes().forEach((b) => { b.checked = on; });
    refresh();
  };
  const pick = (u) => h("input", {
    type: "checkbox", class: "pick", checked: selected.has(u.id), "aria-label": `Select ${u.email || u.name}`,
    onchange: (e) => { e.target.checked ? selected.add(u.id) : selected.delete(u.id); refresh(); },
  });
  const table = sortableTable([
    { label: "", text: (u) => (selected.has(u.id) ? 0 : 1), render: pick },
    { label: "User", text: (u) => u.name || u.email }, { label: "Email", text: (u) => u.email },
    { label: "Effective tier", text: (u) => u.tier },
  ], rows, "none");
  selectAll.onclick = () => setAll(!(rows.length > 0 && selected.size === rows.length));
  assign.onclick = async () => {
    if (!selected.size) return toast("No users are selected", true);
    const tier = tierSel.value;
    if (!confirm(`Assign ${selected.size} user(s) to ${tierLabel(tier)}? Any other default tier they have is replaced.`)) return;
    assign.disabled = true;
    try {
      const r = await api("POST", "/api/audit/assign-default", { tier, user_ids: [...selected] });
      toast(`Assigned ${r.assigned} user(s) to ${tierLabel(tier)}`);
      route();
    } catch (e) {
      toast(e.message, true);
      assign.disabled = false;
    }
  };
  return h("div", {},
    h("div", { class: "toolbar" }, selectAll, assign, tierSel), table);
}

const views = { dashboard: viewDashboard, users: viewUsers, tiers: viewTiers, reports: viewReports, audit: viewAudit };
// URL hash <-> tab, so a refresh or a bookmark keeps the current view.
const HASH_TO_TAB = { dashboard: "dashboard", users: "users", tiers: "tiers", reports: "reports", "tier-audit": "audit" };
const TAB_TO_HASH = Object.fromEntries(Object.entries(HASH_TO_TAB).map(([h, t]) => [t, h]));
const tabFromHash = () => HASH_TO_TAB[decodeURIComponent(location.hash.slice(1))] || "dashboard";
let current = tabFromHash();

let lastRendered = null;

// Shown on every page when the database's tier function predates custom tiers.
async function checkTierFunction() {
  const banner = $("#banner");
  try {
    const s = await api("GET", "/api/status");
    if (s.tier_function_current) return banner.replaceChildren(), (banner.hidden = true);
    banner.replaceChildren(
      h("span", {}, `The database's tier function is out of date: custom tiers are not recognised yet, so their members show as ${tierLabel(cfg.defaultTier)} and get ${tierLabel(cfg.defaultTier)} limits. ` +
        "Update it here, or load the current usage filter into Open WebUI."),
      h("button", { class: "primary", onclick: async () => {
        try { await api("POST", "/api/tier-function"); toast("Tier function updated"); route(); }
        catch (e) { toast(e.message, true); }
      } }, "Update now"));
    banner.hidden = false;
  } catch (e) { /* the view itself reports connection errors */ }
}

async function route() {
  checkTierFunction();
  // Re-rendering the same tab (after an edit) keeps the scroll position; switching tabs starts at the top.
  const scrollY = lastRendered === current ? window.scrollY : 0;
  lastRendered = current;
  charts.forEach((c) => c.destroy()); charts = [];
  const main = $("#main");
  main.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === current));
  const fresh = h("div");
  try { await views[current](fresh); main.replaceChildren(fresh); }
  catch (e) { main.replaceChildren(h("p", { style: "color:var(--bad)" }, e.message)); }
  window.scrollTo(0, scrollY);
}

async function init() {
  cfg = await (await fetch("/config.json")).json();
  if (cfg.environment) $("#env").textContent = cfg.environment;
  const p = new URLSearchParams(location.search);
  if (p.get("error")) { $("#main").textContent = `Sign-in error: ${p.get("error_description") || p.get("error")}`; return; }
  if (p.get("code")) await finishLogin(p.get("code"), p.get("state"));
  token = loadSession();
  if (!token) return login();
  $("#nav").hidden = false;
  $("#logout").hidden = false;
  $("#tz").textContent = `Reporting timezone: ${cfg.timezone}`;
  $("#logout").onclick = logout;
  document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => {
    current = b.dataset.tab;
    if (location.hash !== "#" + TAB_TO_HASH[current]) history.pushState(null, "", "#" + TAB_TO_HASH[current]);
    route();
  }));
  // Back/forward buttons and manually edited hashes.
  window.addEventListener("hashchange", () => { current = tabFromHash(); route(); });
  if (!location.hash) history.replaceState(null, "", "#" + TAB_TO_HASH[current]);
  route();
}

init().catch((e) => { $("#main").textContent = e.message; });
