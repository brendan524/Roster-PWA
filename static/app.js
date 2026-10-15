"use strict";

/* AB Group staff app — Stage 1: login + my roster (list + calendar), admin team view + sheet issues.
   No framework, no build step. All roster text is inserted with textContent (never innerHTML). */

const state = {
  me: null,
  roster: null,       // /api/roster/me
  admin: null,        // /api/admin/roster
  tab: "mine",        // mine | team | issues
  view: localGet("view") || "list",  // list | calendar
  calIndex: 0,        // which of the two months the calendar shows
  selectedDay: null,  // "YYYY-MM-DD"
  showEarlier: false,
  teamStaff: "",
  teamMonth: 0,
  offline: false,
};

const app = document.getElementById("app");
const banner = document.getElementById("banner");
const tabbar = document.getElementById("tabbar");

// ---------- helpers ----------

function localGet(k) { try { return localStorage.getItem("ab." + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("ab." + k, v); } catch { /* private mode */ } }

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: { "x-ab-app": "1", ...(body ? { "content-type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "You're offline. Check your connection and try again.");
  }
  if (method === "GET") state.offline = res.headers.get("x-ab-offline") === "1";
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new ApiError(res.status, (data && data.detail) || "Something went wrong. Please try again.");
  return data;
}

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function parseISO(s) { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); }
function iso(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
function fmtDay(s) { return parseISO(s).toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short" }); }
function fmtDayLong(s) { return parseISO(s).toLocaleDateString("en-AU", { weekday: "long", day: "numeric", month: "long" }); }
function fmtMonth(ym) { const [y, m] = ym.split("-").map(Number); return new Date(y, m - 1, 1).toLocaleDateString("en-AU", { month: "long", year: "numeric" }); }
function fmtTime(hhmm) {
  const [hh, mm] = hhmm.split(":").map(Number);
  const suffix = hh < 12 ? "am" : "pm";
  const h12 = hh % 12 || 12;
  return mm ? `${h12}:${String(mm).padStart(2, "0")}${suffix}` : `${h12}${suffix}`;
}
function fmtHours(n) { return `${Number(n).toFixed(2).replace(/\.?0+$/, "")} h`; }
function fmtAgo(isoTs) {
  const mins = Math.round((Date.now() - new Date(isoTs).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  return new Date(isoTs).toLocaleTimeString("en-AU", { hour: "numeric", minute: "2-digit" });
}
function deptClass(dept) { return "dept-" + (dept || "").toLowerCase().replace(/[^a-z0-9]+/g, "-"); }
function groupByDate(shifts) {
  const map = new Map();
  for (const s of shifts) { if (!map.has(s.date)) map.set(s.date, []); map.get(s.date).push(s); }
  return map;
}

function render(...nodes) { app.replaceChildren(...nodes.flat().filter((n) => n != null && n !== false)); }

// ---------- screens: auth ----------

function renderLogin(message) {
  setChrome(false);
  const err = h("p", { class: "form-error", hidden: !message, role: "alert" }, message || "");
  const email = h("input", { type: "text", id: "email", autocomplete: "username", required: true,
    autocapitalize: "none", autocorrect: "off", spellcheck: "false", placeholder: "e.g. AB0010" });
  const pw = h("input", { type: "password", id: "password", autocomplete: "current-password", required: true });
  const btn = h("button", { class: "btn", type: "submit" }, "Log in");
  const form = h("form", {
    class: "card auth", onsubmit: async (e) => {
      e.preventDefault();
      btn.disabled = true; err.hidden = true;
      try {
        state.me = await api("/api/auth/login", { method: "POST", body: { login: email.value, password: pw.value } });
        await start();
      } catch (ex) {
        err.textContent = ex.message; err.hidden = false; btn.disabled = false; pw.value = ""; pw.focus();
      }
    },
  },
    h("h1", {}, "Welcome"),
    h("p", { class: "sub" }, "Log in to see your shifts."),
    h("label", { for: "email" }, "Staff ID or email"), email,
    h("label", { for: "password" }, "Password"), pw,
    err, btn,
    h("p", { class: "hint" }, "Forgotten your password? Ask Brendan, Aaron or Dylan to reset it."),
  );
  render(h("div", { class: "hero" }, h("img", { src: "/logo.png", alt: "The AB Group" })), form);
  email.focus();
}

function renderChangePassword() {
  setChrome(true);
  const err = h("p", { class: "form-error", hidden: true, role: "alert" });
  const cur = h("input", { type: "password", id: "cur", autocomplete: "current-password", required: true });
  const nw = h("input", { type: "password", id: "new", autocomplete: "new-password", required: true, minlength: "10" });
  const again = h("input", { type: "password", id: "again", autocomplete: "new-password", required: true });
  const btn = h("button", { class: "btn", type: "submit" }, "Save password");
  const form = h("form", {
    class: "card auth", onsubmit: async (e) => {
      e.preventDefault();
      err.hidden = true;
      if (nw.value !== again.value) { err.textContent = "The new passwords don't match."; err.hidden = false; return; }
      btn.disabled = true;
      try {
        state.me = await api("/api/auth/change-password", { method: "POST", body: { current_password: cur.value, new_password: nw.value } });
        await start();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; }
    },
  },
    h("h1", {}, "Set your password"),
    h("p", { class: "sub" }, "Choose a new password to replace the temporary one you were given."),
    h("label", { for: "cur" }, "Temporary password"), cur,
    h("label", { for: "new" }, "New password"), nw,
    h("p", { class: "hint" }, "At least 10 characters."),
    h("label", { for: "again" }, "New password again"), again,
    err, btn,
  );
  render(form);
  cur.focus();
}

// ---------- chrome ----------

function setChrome(loggedIn) {
  document.body.classList.toggle("auth-screen", !loggedIn);
  document.getElementById("userbox").hidden = !loggedIn;
  document.getElementById("username").textContent = loggedIn && state.me ? state.me.name : "";
  const showTabs = loggedIn && state.me && state.me.role === "admin" && !state.me.must_change_password;
  tabbar.hidden = !showTabs;
  if (!loggedIn) banner.hidden = true;
}

function renderTabbar() {
  if (tabbar.hidden) return;
  const errors = state.admin ? state.admin.issues.filter((i) => i.level === "error").length : 0;
  const tab = (id, label, extra) => h("button", {
    "aria-current": state.tab === id ? "page" : false,
    onclick: () => { state.tab = id; route(); },
  }, label, extra);
  tabbar.replaceChildren(
    tab("mine", "My shifts"),
    tab("team", "Team"),
    tab("issues", "Sheet issues", errors ? h("span", { class: "badge" }, errors) : null),
  );
}

function setBanner(data) {
  if (state.offline) {
    banner.textContent = `You're offline — showing your roster from ${fmtAgo(data.fetched_at)}.`;
    banner.hidden = false;
  } else if (data && data.stale) {
    banner.textContent = `Couldn't reach the roster just now — showing the copy from ${fmtAgo(data.fetched_at)}.`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

function pageHead(title, data, onRefresh) {
  const btn = h("button", {
    class: "icon-btn", onclick: async () => {
      btn.disabled = true; btn.textContent = "Refreshing…";
      try { await onRefresh(); } finally { btn.disabled = false; btn.textContent = "Refresh"; }
    },
  }, "Refresh");
  return h("div", { class: "page-head" },
    h("h1", {}, title),
    h("div", { class: "updated" }, data ? `Updated ${fmtAgo(data.fetched_at)}` : "", btn),
  );
}

// ---------- my roster ----------

async function loadMine(refresh = false) {
  state.roster = await api("/api/roster/me" + (refresh ? "?refresh=true" : ""));
}

function shiftCard(s, { past = false, showWho = false } = {}) {
  const meta = [s.location, s.department].filter(Boolean);
  return h("article", { class: `shift ${deptClass(s.department)} ${past ? "past" : ""}` },
    showWho ? h("div", { class: "who" }, s.staff_name || s.staff_id, " · ", s.staff_id) : null,
    h("div", { class: "row1" },
      h("span", { class: "time" }, `${fmtTime(s.start)} – ${fmtTime(s.end)}`, s.overnight ? " (next day)" : "",
        s.local ? h("span", { class: "tz" }, " " + ((state.roster && state.roster.sheet_tz_label) || "")) : null),
      h("span", { class: "hrs" }, fmtHours(s.scheduled_hours),
        s.teaching_hours != null ? ` · ${fmtHours(s.teaching_hours)} teaching` : ""),
    ),
    s.local ? h("div", { class: "localtime" }, localLine(s)) : null,
    s.role ? h("div", { class: "role" }, s.role) : null,
    meta.length ? h("div", { class: "meta" }, s.location || "", s.department ? h("span", { class: "chip" }, s.department) : null) : null,
    s.notes ? h("div", { class: "notes" }, s.notes) : null,
  );
}

// Offshore staff: the sheet's (Melbourne) times plus the same shift in their local time.
function localLine(s) {
  const r = state.roster || {};
  const day = s.local.date !== s.date ? `${fmtDay(s.local.date)}, ` : "";
  const next = s.local.end_next_day ? " (next day)" : "";
  return `${day}${fmtTime(s.local.start)} – ${fmtTime(s.local.end)}${next} ${r.local_tz_label || "local time"}`;
}

function emptyCard(title, sub) {
  return h("div", { class: "empty" }, h("strong", {}, title), sub || "");
}

function nowHHMM() { const d = new Date(); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; }

function isPast(s, today) {
  if (s.date < today) return true;
  return s.date === today && !s.overnight && s.end <= nowHHMM();
}

function renderMine() {
  const r = state.roster;
  setBanner(r);
  const today = r.today;
  const all = r.months.flatMap((m) => m.shifts);
  const next = all.find((s) => !isPast(s, today));

  const seg = h("div", { class: "segmented", role: "group", "aria-label": "View" },
    ["list", "calendar"].map((v) => h("button", {
      "aria-pressed": state.view === v ? "true" : "false",
      onclick: () => { state.view = v; localSet("view", v); renderMine(); },
    }, v === "list" ? "List" : "Calendar")),
  );

  const nextCard = next ? h("section", { class: "next" },
    h("div", { class: "eyebrow" }, next.date === today ? "Today" : "Next shift"),
    h("div", { class: "when" }, `${fmtDay(next.date)} · ${fmtTime(next.start)} – ${fmtTime(next.end)}`,
      next.local ? h("span", { class: "tz" }, " " + (r.sheet_tz_label || "")) : null),
    next.local ? h("div", { class: "local" }, localLine(next)) : null,
    h("div", { class: "what" }, [next.role, next.location].filter(Boolean).join(" · ")),
  ) : null;

  const body = state.view === "list" ? renderList(r, today) : renderCalendar(r, today);
  render(
    pageHead("My shifts", r, async () => { await loadMine(true); renderMine(); }),
    seg,
    state.view === "list" ? nextCard : null,
    body,
  );
}

function renderList(r, today) {
  const out = [];
  r.months.forEach((m, idx) => {
    out.push(h("h2", { class: "month-title" }, fmtMonth(m.month)));
    if (!m.shifts.length) {
      out.push(emptyCard("No shifts scheduled yet", idx === 0 ? "" : "Check back closer to the time."));
      return;
    }
    const past = m.shifts.filter((s) => isPast(s, today));
    const upcoming = m.shifts.filter((s) => !isPast(s, today));
    if (past.length) {
      if (state.showEarlier) {
        for (const [d, list] of groupByDate(past)) out.push(dayBlock(d, list, today, true));
      } else {
        out.push(h("button", { class: "icon-btn show-earlier", onclick: () => { state.showEarlier = true; renderMine(); } },
          `Show ${past.length} earlier shift${past.length === 1 ? "" : "s"}`));
      }
    }
    if (!upcoming.length) out.push(emptyCard("No more shifts this month"));
    for (const [d, list] of groupByDate(upcoming)) out.push(dayBlock(d, list, today, false));
  });
  return h("div", {}, out);
}

function dayBlock(d, list, today, past, showWho = false) {
  return h("section", { class: "day" },
    h("h3", { class: "day-label" }, fmtDayLong(d), d === today ? h("span", { class: "today-pill" }, "Today") : null),
    list.map((s) => shiftCard(s, { past, showWho })),
  );
}

function renderCalendar(r, today) {
  const idx = Math.min(state.calIndex, r.months.length - 1);
  const m = r.months[idx];
  const [y, mo] = m.month.split("-").map(Number);
  const first = new Date(y, mo - 1, 1);
  const days = new Date(y, mo, 0).getDate();
  const byDate = groupByDate(m.shifts);

  if (!state.selectedDay || !state.selectedDay.startsWith(m.month)) {
    state.selectedDay = today.startsWith(m.month) ? today : (m.shifts[0] ? m.shifts[0].date : null);
  }

  const cells = DOW.map((d) => h("div", { class: "cal-dow" }, d));
  const lead = (first.getDay() + 6) % 7; // Monday-first
  for (let i = 0; i < lead; i++) cells.push(h("div", { class: "cal-day blank" }));
  for (let d = 1; d <= days; d++) {
    const key = iso(new Date(y, mo - 1, d));
    const list = byDate.get(key) || [];
    const cls = ["cal-day", key < today && "past", key === today && "today", list.length && "has", key === state.selectedDay && "selected"]
      .filter(Boolean).join(" ");
    cells.push(h("button", {
      class: cls,
      "aria-label": `${fmtDayLong(key)}${list.length ? `, ${list.length} shift${list.length > 1 ? "s" : ""}` : ""}`,
      "aria-pressed": key === state.selectedDay ? "true" : "false",
      onclick: () => { state.selectedDay = key; renderMine(); },
    }, String(d), h("span", { class: "dots" },
      list.slice(0, 3).map((s) => h("i", { class: /first aid/i.test(s.department) ? "fa" : "" })))));
  }

  const sel = state.selectedDay;
  const selList = sel ? byDate.get(sel) || [] : [];
  const detail = h("div", { class: "cal-detail" },
    !m.shifts.length ? emptyCard("No shifts scheduled yet", fmtMonth(m.month))
      : sel ? (selList.length ? dayBlock(sel, selList, today, false) : emptyCard("No shifts", fmtDayLong(sel)))
        : null,
  );

  return h("div", {},
    h("div", { class: "cal" },
      h("div", { class: "cal-head" },
        h("button", { class: "cal-nav", "aria-label": "Previous month", disabled: idx === 0,
          onclick: () => { state.calIndex = idx - 1; state.selectedDay = null; renderMine(); } }, "‹"),
        h("h2", {}, fmtMonth(m.month)),
        h("button", { class: "cal-nav", "aria-label": "Next month", disabled: idx >= r.months.length - 1,
          onclick: () => { state.calIndex = idx + 1; state.selectedDay = null; renderMine(); } }, "›"),
      ),
      h("div", { class: "cal-grid" }, cells),
    ),
    detail,
  );
}

// ---------- admin ----------

async function loadAdmin(refresh = false) {
  state.admin = await api("/api/admin/roster" + (refresh ? "?refresh=true" : ""));
}

function renderTeam() {
  const a = state.admin;
  setBanner(a);
  const staffSel = h("select", { "aria-label": "Staff member", onchange: (e) => { state.teamStaff = e.target.value; renderTeam(); } },
    h("option", { value: "" }, "Everyone"),
    a.staff.map((s) => h("option", { value: s.staff_id, selected: s.staff_id === state.teamStaff }, `${s.name} (${s.staff_id})`)),
  );
  const monthSel = h("select", { "aria-label": "Month", onchange: (e) => { state.teamMonth = Number(e.target.value); renderTeam(); } },
    a.months.map((m, i) => h("option", { value: String(i), selected: i === state.teamMonth }, fmtMonth(m.month))),
  );
  const m = a.months[state.teamMonth] || a.months[0];
  const shifts = m.shifts.filter((s) => !state.teamStaff || s.staff_id === state.teamStaff);
  const total = shifts.reduce((n, s) => n + s.scheduled_hours, 0);

  const blocks = [];
  if (!m.published) blocks.push(emptyCard(`No ${m.month} tab in the sheet yet`, "Staff will see “No shifts scheduled yet”."));
  else if (!shifts.length) blocks.push(emptyCard("No shifts scheduled yet"));
  for (const [d, list] of groupByDate(shifts)) blocks.push(dayBlock(d, list, a.today, d < a.today, true));

  render(
    pageHead("Team roster", a, async () => { await loadAdmin(true); renderTabbar(); renderTeam(); }),
    h("div", { class: "filters" }, staffSel, monthSel),
    h("div", { class: "summary" },
      h("span", { class: "ok" }, `${shifts.length} shift${shifts.length === 1 ? "" : "s"} · ${fmtHours(total)}`)),
    ...blocks,
  );
}

function renderIssues() {
  const a = state.admin;
  setBanner(a);
  const errors = a.issues.filter((i) => i.level === "error");
  const warnings = a.issues.filter((i) => i.level === "warning");
  const list = a.issues.length ? a.issues.map((i) => h("article", { class: `issue ${i.level}` },
    h("div", { class: "where" }, `Tab ${i.tab}${i.row ? ` · row ${i.row}` : ""}${i.staff_id ? ` · ${i.staff_id}` : ""}`),
    h("div", {}, h("span", { class: "lvl" }, i.level === "error" ? "Not shown: " : "Check: "), i.message.replace(/^Shift not shown: /, "")),
  )) : [emptyCard("No problems found", "Every row in the roster sheet was read cleanly.")];

  render(
    pageHead("Sheet issues", a, async () => { await loadAdmin(true); renderTabbar(); renderIssues(); }),
    h("div", { class: "summary" },
      errors.length ? h("span", { class: "e" }, `${errors.length} not shown to staff`) : null,
      warnings.length ? h("span", { class: "w" }, `${warnings.length} to check`) : null,
      a.refresh_error ? h("span", { class: "e" }, a.refresh_error) : null,
    ),
    h("p", { class: "hint" }, "Fix these in the Google Sheet, then tap Refresh. Rows marked “Not shown” are missing from someone's roster."),
    ...list,
  );
}

// ---------- routing ----------

async function route() {
  renderTabbar();
  try {
    if (state.tab === "mine") {
      if (!state.roster) { render(h("p", { class: "loading" }, "Loading your shifts…")); await loadMine(); }
      renderMine();
    } else {
      if (!state.admin) { render(h("p", { class: "loading" }, "Loading roster…")); await loadAdmin(); renderTabbar(); }
      state.tab === "team" ? renderTeam() : renderIssues();
    }
  } catch (ex) {
    if (ex.status === 401) return renderLogin();
    render(h("div", { class: "empty" }, h("strong", {}, "Couldn't load the roster"), ex.message,
      h("button", { class: "btn", onclick: () => route() }, "Try again")));
  }
}

async function start() {
  state.roster = null; state.admin = null; state.tab = "mine";
  if (state.me.must_change_password) return renderChangePassword();
  setChrome(true);
  await route();
  if (state.me.role === "admin") loadAdmin().then(renderTabbar).catch(() => {});
}

async function boot() {
  try {
    state.me = await api("/api/auth/me");
    await start();
  } catch (ex) {
    renderLogin(ex.status === 0 ? ex.message : null);
  }
}

document.getElementById("logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* offline: still clear locally */ }
  if (window.caches) caches.delete("ab-data").catch(() => {});
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: "logout" });
  }
  state.me = null; state.roster = null; state.admin = null;
  renderLogin();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.me && !state.me.must_change_password) {
    if (state.tab === "mine") loadMine().then(renderMine).catch(() => {});
  }
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

boot();
