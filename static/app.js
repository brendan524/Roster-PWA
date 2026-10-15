"use strict";

/* AB Group staff app: login, my roster (list + calendar), clock in/out with PIN + GPS;
   admins also get the clock day view, team roster and sheet issues.
   No framework, no build step. All roster text is inserted with textContent (never innerHTML). */

const state = {
  me: null,
  roster: null,       // /api/roster/me
  admin: null,        // /api/admin/roster
  tab: "mine",        // mine | leave | (admins) clock | team | issues
  view: localGet("view") || "list",  // list | calendar
  calIndex: 0,        // which of the two months the calendar shows
  selectedDay: null,  // "YYYY-MM-DD"
  showEarlier: false,
  teamStaff: "",
  teamMonth: 0,
  offline: false,
  clock: null,        // /api/clock/me
  clockBusy: false,
  clockMsg: null,     // {type: "ok"|"warn"|"error", text}
  pending: 0,         // punches waiting in this device's outbox
  clockDay: null,     // admin clock view: "YYYY-MM-DD"
  clockData: null,    // /api/admin/clock
  fixOpen: null,      // which admin card has its fix form open
  leave: null,        // /api/leave/me
  leaveAdmin: null,   // /api/admin/leave
  leaveFormOpen: false,
  leaveMsg: null,
  xero: null,         // /api/admin/xero/status (admins)
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

function render(...nodes) { app.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null && n !== false)); }

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
  const showTabs = loggedIn && state.me && !state.me.must_change_password;
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
  const pendingLeave = state.leaveAdmin ? state.leaveAdmin.pending.length : 0;
  if (state.me.role !== "admin") {
    tabbar.replaceChildren(tab("mine", "My shifts"), tab("leave", "Leave"));
    return;
  }
  tabbar.replaceChildren(
    tab("mine", "My shifts"),
    tab("clock", "Clock"),
    tab("leave", "Leave", pendingLeave ? h("span", { class: "badge" }, pendingLeave) : null),
    tab("team", "Team"),
    tab("issues", "Issues", errors ? h("span", { class: "badge" }, errors) : null),
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
  const [roster, clock] = await Promise.all([
    api("/api/roster/me" + (refresh ? "?refresh=true" : "")),
    api("/api/clock/me").catch(() => state.clock),
  ]);
  state.roster = roster;
  state.clock = clock;
  await refreshPending();
}

// ---------- clock in / out (personal device) ----------

const STALE_CLOCK_IN_MS = 16 * 3600 * 1000;
const mineFilter = (item) => item.url === "/api/clock/punch" && state.me && item.staff_id === state.me.staff_id;

async function refreshPending() {
  state.pending = await ABClock.pending(mineFilter);
}

function setClockMsg(type, text) {
  state.clockMsg = { type, text, at: Date.now() };
  setTimeout(() => {
    if (state.clockMsg && Date.now() - state.clockMsg.at >= 9000) { state.clockMsg = null; if (state.tab === "mine" && state.roster) renderMine(); }
  }, 9500);
}

async function flushMine() {
  if (!state.me) return;
  const before = state.pending;
  await ABClock.flush(mineFilter, (item, reason) => {
    setClockMsg("error", `Your clock-${item.body.kind} from ${ABClock.fmtDayTime(new Date(item.body.device_time).toISOString())} couldn't be saved: ${reason} Please tell your manager.`);
  });
  await refreshPending();
  if (before && !state.pending) {
    try { state.clock = await api("/api/clock/me"); } catch { /* still offline */ }
  }
  if (state.tab === "mine" && state.roster) renderMine();
}

async function doPunch(kind) {
  const geoPromise = ABClock.startGeo();
  const pin = await ABClock.pinPad({ title: kind === "in" ? "Enter your PIN to clock in" : "Enter your PIN to clock out" });
  if (!pin) return;
  const pressedAt = Date.now();
  state.clockBusy = true; state.clockMsg = null; renderMine();
  // Use the location if it's ready (or arrives within 4s); never hold the punch up for it.
  const geo = await Promise.race([geoPromise, new Promise((r) => setTimeout(() => r({ status: "timeout" }), 4000))]);
  const client_id = ABClock.uuid();
  const res = await ABClock.submit({
    client_id, url: "/api/clock/punch", staff_id: state.me.staff_id,
    body: { client_id, kind, pin, device_time: pressedAt, geo },
  });
  state.clockBusy = false;
  const word = kind === "in" ? "in" : "out";
  if (res.result === "sent") {
    const d = res.data;
    state.clock = { ...(state.clock || {}), clocked_in: d.clocked_in, since: d.since };
    setClockMsg("ok", d.duplicate ? `Already clocked ${word} at ${ABClock.fmtTime(d.occurred_at)}.` : `Clocked ${word} at ${ABClock.fmtTime(d.occurred_at)}.`);
    api("/api/clock/me").then((c) => { state.clock = c; renderMine(); }).catch(() => {});
  } else if (res.result === "queued") {
    state.clock = { ...(state.clock || {}), clocked_in: kind === "in", since: kind === "in" ? new Date(pressedAt).toISOString() : null };
    setClockMsg("warn", `No connection. Your clock-${word} at ${ABClock.fmtTime(new Date(pressedAt).toISOString())} is saved on this phone and will send automatically.`);
  } else {
    setClockMsg("error", res.message);
  }
  await refreshPending();
  renderMine();
}

function clockCard() {
  const c = state.clock;
  if (!c) return null;
  if (!c.has_pin) {
    return h("section", { class: "clock-card" }, h("div", { class: "clock-status" }, "Clock in/out needs a PIN. Ask Brendan, Aaron or Dylan to set one up for you."));
  }
  const stale = c.clocked_in && c.since && Date.now() - new Date(c.since).getTime() > STALE_CLOCK_IN_MS;
  const isIn = c.clocked_in && !stale;
  const kind = isIn ? "out" : "in";
  const today = new Date().toDateString();
  const todays = (c.recent || []).filter((p) => new Date(p.occurred_at).toDateString() === today).reverse();
  const msg = state.clockMsg;
  return h("section", { class: `clock-card ${isIn ? "is-in" : ""}` },
    h("div", { class: "clock-status" },
      h("span", { class: "dot", "aria-hidden": "true" }),
      isIn ? `Clocked in since ${ABClock.fmtTime(c.since)}` : "Not clocked in"),
    stale ? h("p", { class: "clock-note" }, `Your clock-in from ${ABClock.fmtDayTime(c.since)} has no clock-out. Your manager will fix it — just clock in as normal.`) : null,
    h("button", {
      class: `btn-clock ${kind}`, disabled: state.clockBusy,
      onclick: () => doPunch(kind),
    }, state.clockBusy ? "Saving…" : kind === "in" ? "Clock in" : "Clock out"),
    msg ? h("p", { class: `clock-msg ${msg.type}`, role: "status" }, msg.text) : null,
    state.pending ? h("p", { class: "clock-pending" }, `${state.pending} waiting to send — it'll go automatically when you're back online.`) : null,
    todays.length ? h("p", { class: "clock-today" }, "Today: ",
      todays.map((p) => `${p.kind === "in" ? "In" : "Out"} ${ABClock.fmtTime(p.occurred_at)}`).join(" · ")) : null,
  );
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
    clockCard(),
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
  // One card per person + problem, listing every row it affects, so 40 identical
  // "Staff ID is empty" rows for one person become a single to-do.
  const groups = new Map();
  for (const i of a.issues) {
    const msg = i.message.replace(/^Shift not shown: /, "");
    const who = [i.staff_name, i.staff_id].filter(Boolean).join(" · ");
    const key = `${i.level}|${who}|${msg}`;
    if (!groups.has(key)) groups.set(key, { level: i.level, who, msg, rows: new Map(), count: 0 });
    const g = groups.get(key);
    g.count += 1;
    if (!g.rows.has(i.tab)) g.rows.set(i.tab, []);
    if (i.row) g.rows.get(i.tab).push(i.row);
  }
  const sorted = [...groups.values()].sort((x, y) =>
    (x.level === "error" ? 0 : 1) - (y.level === "error" ? 0 : 1) || y.count - x.count);
  const list = sorted.length ? sorted.map((g) => h("article", { class: `issue ${g.level}` },
    h("div", { class: "where" }, g.who || "No name on row", g.count > 1 ? ` · ${g.count} rows` : ""),
    h("div", {}, h("span", { class: "lvl" }, g.level === "error" ? "Not shown: " : "Check: "), g.msg),
    h("div", { class: "rows" }, [...g.rows].map(([tab, rows]) =>
      `Tab ${tab}${rows.length ? ` · row${rows.length > 1 ? "s" : ""} ${rows.join(", ")}` : ""}`).join("   ")),
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

// ---------- leave ----------

async function loadLeave() {
  const isAdmin = state.me.role === "admin";
  const [mine, admin, xero] = await Promise.all([
    api("/api/leave/me"),
    isAdmin ? api("/api/admin/leave") : Promise.resolve(null),
    isAdmin ? api("/api/admin/xero/status").catch(() => null) : Promise.resolve(null),
  ]);
  state.leave = mine;
  state.leaveAdmin = admin;
  state.xero = xero;
}

function fmtUnits(n, unit) {
  const v = Math.round(n * 100) / 100;
  const u = (unit || "hours").toLowerCase();
  return `${v} ${v === 1 ? u.replace(/s$/, "") : u}`;
}

function xeroBalanceCards(list) {
  return [
    h("div", { class: "bal-grid" }, list.map((b) => h("div", { class: "bal" },
      h("div", { class: "bal-label" }, b.name),
      h("div", { class: "bal-num" }, String(Math.round(b.units * 100) / 100), h("span", {}, ` ${(b.unit_type || "hours").toLowerCase()}`)),
    ))),
    h("p", { class: "hint" }, `From Xero, updated ${fmtAgo(list[0].fetched_at)}. Requests you make here are recorded; Xero is updated by payroll.`),
  ];
}

function xeroPanel(X) {
  const reload = async () => { state.xero = await api("/api/admin/xero/status"); renderLeave(); };
  if (!X.configured) {
    return h("div", { class: "card xero" }, h("b", {}, "Xero (onshore balances)"),
      h("p", { class: "hint" }, "Not set up on the server yet: add XERO_CLIENT_ID and XERO_CLIENT_SECRET to .env (see README → Xero)."));
  }
  if (!X.connected) {
    return h("div", { class: "card xero" }, h("b", {}, "Xero (onshore balances)"),
      h("p", { class: "hint" }, "Connect once so onshore staff can see their leave balances here. You'll sign in to Xero and approve read-only payroll access."),
      h("a", { class: "btn", href: "/api/admin/xero/connect" }, "Connect Xero"));
  }
  const opts = (sel) => [h("option", { value: "" }, "— not linked —"),
    ...X.xero_employees.map((e) => h("option", { value: e.id, selected: sel === e.id }, e.name))];
  const syncBtn = h("button", { class: "icon-btn", onclick: async () => {
    syncBtn.disabled = true; syncBtn.textContent = "Syncing…";
    try { await api("/api/admin/xero/sync", { method: "POST" }); } catch (ex) { alert(ex.message); }
    await reload();
  } }, "Sync now");
  return h("div", { class: "card xero" },
    h("div", { class: "row1" }, h("b", {}, `Xero: ${X.tenant_name}`), syncBtn),
    h("p", { class: "hint" }, X.last_sync_at ? `Last synced ${fmtAgo(X.last_sync_at)} (automatically every few hours).` : "Not synced yet."),
    X.last_error ? h("p", { class: "form-error" }, X.last_error, " ", h("a", { href: "/api/admin/xero/connect" }, "Reconnect")) : null,
    h("h3", { class: "xero-h" }, "Onshore staff ↔ Xero employees"),
    X.staff.map((st) => h("div", { class: "xero-row" },
      h("div", {}, h("b", {}, st.name),
        st.balances.length ? h("div", { class: "meta" }, st.balances.map((b) => `${b.name}: ${fmtUnits(b.units, b.unit_type)}`).join(" · "))
          : h("div", { class: "meta" }, st.link ? "No balances in Xero" : "Not linked — pick their Xero name")),
      X.xero_employees.length ? h("select", { "aria-label": `Xero employee for ${st.name}`, onchange: async (e) => {
        try { await api("/api/admin/xero/link", { method: "POST", body: { staff_id: st.staff_id, xero_employee_id: e.target.value || null } });
          await api("/api/admin/xero/sync", { method: "POST" }).catch(() => {}); } catch (ex) { alert(ex.message); }
        await reload();
      } }, opts(st.link && st.link.xero_employee_id)) : null,
    )),
    h("button", { class: "link-btn dark", onclick: async () => {
      if (!confirm("Disconnect Xero? Onshore balances will stop showing until you connect again.")) return;
      await api("/api/admin/xero/disconnect", { method: "POST" }); await reload();
    } }, "Disconnect Xero"),
  );
}

function fmtDays(n) { const v = Math.round(n * 10) / 10; return `${v} day${v === 1 ? "" : "s"}`; }
function fmtRange(r) {
  const a = fmtDay(r.start_date), b = fmtDay(r.end_date);
  const halves = [r.start_half ? "half first day" : "", r.end_half && r.end_date !== r.start_date ? "half last day" : ""].filter(Boolean);
  return `${r.start_date === r.end_date ? a : `${a} – ${b}`}${halves.length ? ` (${halves.join(", ")})` : ""}`;
}

// Mirrors the server's count (Mon–Fri, optional half days) for a live preview.
function previewDays(start, end, sh, eh) {
  if (!start || !end || end < start) return 0;
  let n = 0;
  for (let d = parseISO(start); iso(d) <= end; d.setDate(d.getDate() + 1)) if (d.getDay() % 6 !== 0) n += 1;
  const wk = (x) => parseISO(x).getDay() % 6 !== 0;
  if (sh && wk(start)) n -= 0.5;
  if (eh && end !== start && wk(end)) n -= 0.5;
  return Math.max(n, 0);
}

const STATUS_LABEL = { pending: "Waiting for approval", approved: "Approved", declined: "Declined", cancelled: "Cancelled" };

function balanceCards(bals) {
  return h("div", { class: "bal-grid" }, bals.map((b) => h("div", { class: "bal" },
    h("div", { class: "bal-label" }, b.label),
    h("div", { class: "bal-num" }, String(Math.round(b.available * 10) / 10), h("span", {}, " days")),
    h("div", { class: "bal-sub" }, "available to book"),
    b.booked ? h("div", { class: "bal-sub" }, `${fmtDays(b.booked)} booked ahead`) : null,
    b.pending ? h("div", { class: "bal-sub" }, `${fmtDays(b.pending)} waiting for approval`) : null,
    b.cap != null && b.balance >= b.cap ? h("div", { class: "bal-sub warn" }, `At the ${b.cap}-day maximum — use some to keep earning`) : null,
    b.next_date ? h("div", { class: "bal-next" }, `+${b.next_amount} on ${fmtDay(b.next_date)}`) : null,
  )));
}

function leaveForm(L) {
  const avail = Object.fromEntries((L.balances || []).map((b) => [b.code, b.available]));
  const err = h("p", { class: "form-error", hidden: true, role: "alert" });
  const type = h("select", { id: "lt" }, L.types.map((t) => h("option", { value: t.code }, t.label)));
  const start = h("input", { type: "date", id: "ls", min: shiftDays(L.today, -60), required: true });
  const end = h("input", { type: "date", id: "le", required: true });
  const sh = h("input", { type: "checkbox", id: "lsh" });
  const eh = h("input", { type: "checkbox", id: "leh" });
  const reason = h("input", { type: "text", id: "lr", maxlength: "500", placeholder: "Optional" });
  const preview = h("p", { class: "leave-preview" });
  const ehWrap = h("label", { class: "check", for: "leh" }, eh, " Last day is a half day");
  const update = () => {
    if (start.value && (!end.value || end.value < start.value)) end.value = start.value;
    end.min = start.value || "";
    ehWrap.hidden = !end.value || end.value === start.value;
    const days = previewDays(start.value, end.value, sh.checked, eh.checked);
    const a = avail[type.value];
    preview.className = "leave-preview";
    if (!start.value) { preview.textContent = ""; return; }
    if (!days) { preview.textContent = "No working days (Mon–Fri) in those dates."; preview.classList.add("warn"); return; }
    let text = `${fmtDays(days)} of leave.`;
    if (a != null && days > a) { text += ` That's ${fmtDays(days - a)} more than you have available — you can still ask; your manager will decide.`; preview.classList.add("warn"); }
    preview.textContent = text;
  };
  [type, start, end, sh, eh].forEach((el) => el.addEventListener("change", update));
  const btn = h("button", { class: "btn", type: "submit" }, "Send request");
  return h("form", {
    class: "card leave-form", onsubmit: async (e) => {
      e.preventDefault(); err.hidden = true; btn.disabled = true;
      try {
        await api("/api/leave/requests", { method: "POST", body: {
          leave_type: type.value, start_date: start.value, end_date: end.value,
          start_half: sh.checked, end_half: !ehWrap.hidden && eh.checked, reason: reason.value,
        } });
        state.leaveFormOpen = false;
        state.leaveMsg = "Request sent. You'll see it here once it's approved.";
        await loadLeave(); renderTabbar(); renderLeave();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; }
    },
  },
    h("label", { for: "lt" }, "Type of leave"), type,
    h("div", { class: "date-row" },
      h("div", {}, h("label", { for: "ls" }, "From"), start),
      h("div", {}, h("label", { for: "le" }, "To"), end)),
    h("label", { class: "check", for: "lsh" }, sh, " First day is a half day"),
    ehWrap,
    h("label", { for: "lr" }, "Reason"), reason,
    preview, err, btn,
    h("button", { class: "link-btn dark", type: "button", onclick: () => { state.leaveFormOpen = false; renderLeave(); } }, "Cancel"),
  );
}

function requestCard(r, { admin = false } = {}) {
  const note = r.balance && r.balance.short_by ? h("div", { class: "flags" },
    h("span", { class: "flag w" }, `${fmtDays(r.balance.short_by)} more than available (${fmtDays(r.balance.available)})`)) : null;
  const balInfo = admin && r.balance && !r.balance.short_by
    ? h("div", { class: "meta" }, `Balance after: ${fmtDays(r.balance.after)} (available now ${fmtDays(r.balance.available)})`)
    : admin && r.balance_note ? h("div", { class: "meta" }, r.balance_note) : null;
  const decided = r.decided_by && r.status !== "pending" && r.status !== "cancelled"
    ? h("div", { class: "meta" }, `${STATUS_LABEL[r.status]} by ${r.decided_by}${r.decision_note ? ` — “${r.decision_note}”` : ""}`) : null;
  return h("article", { class: `leave-req st-${r.status}` },
    h("div", { class: "row1" }, h("b", {}, admin ? r.name : r.type_label), h("span", { class: `pill ${r.status}` }, STATUS_LABEL[r.status])),
    admin ? h("div", { class: "meta" }, r.type_label) : null,
    h("div", {}, `${fmtRange(r)} · ${fmtDays(r.days)}`),
    r.reason ? h("div", { class: "meta" }, `“${r.reason}”`) : null,
    note, balInfo, decided,
    !admin && r.status === "pending" ? h("button", { class: "link-btn dark", onclick: async () => {
      if (!confirm("Cancel this leave request?")) return;
      try { await api(`/api/leave/requests/${r.id}/cancel`, { method: "POST" }); await loadLeave(); renderTabbar(); renderLeave(); }
      catch (ex) { alert(ex.message); }
    } }, "Cancel request") : null,
    admin && r.status === "pending" ? decisionRow(r) : null,
  );
}

function decisionRow(r) {
  const note = h("input", { type: "text", placeholder: "Note to them (optional)", maxlength: "500" });
  const go = (decision) => async (e) => {
    e.target.disabled = true;
    try { await api(`/api/admin/leave/${r.id}/decide`, { method: "POST", body: { decision, note: note.value } }); await loadLeave(); renderTabbar(); renderLeave(); }
    catch (ex) { alert(ex.message); e.target.disabled = false; }
  };
  return h("div", { class: "decide" }, note,
    h("div", { class: "decide-btns" },
      h("button", { class: "btn small approve", onclick: go("approve") }, "Approve"),
      h("button", { class: "btn small decline", onclick: go("decline") }, "Decline")));
}

function renderLeave() {
  const L = state.leave;
  const A = state.leaveAdmin;
  banner.hidden = true;
  const upcoming = L.requests.filter((r) => r.end_date >= L.today || r.status === "pending");
  const past = L.requests.filter((r) => !upcoming.includes(r));
  let balanceBlock;
  if (L.balances) balanceBlock = balanceCards(L.balances);
  else if (L.location_type === "offshore") balanceBlock = emptyCard("Balance not set up yet", "It'll show once your start date is added. You can still apply.");
  else if (L.xero_balances) balanceBlock = xeroBalanceCards(L.xero_balances);
  else balanceBlock = h("p", { class: "hint" }, "Your leave balances are kept in Xero. Ask Brendan or Aaron for your current balance.");

  render(
    h("div", { class: "page-head" }, h("h1", {}, "Leave"),
      h("div", { class: "updated" }, h("button", { class: "icon-btn", onclick: async () => { await loadLeave(); renderTabbar(); renderLeave(); } }, "Refresh"))),
    A && A.pending.length ? [h("h2", { class: "month-title" }, `Waiting for your approval (${A.pending.length})`),
      ...A.pending.map((r) => requestCard(r, { admin: true }))] : null,
    A ? h("h2", { class: "month-title" }, "My leave") : null,
    balanceBlock,
    state.leaveMsg ? h("p", { class: "clock-msg ok", role: "status" }, state.leaveMsg) : null,
    state.leaveFormOpen ? leaveForm(L)
      : h("button", { class: "btn", onclick: () => { state.leaveFormOpen = true; state.leaveMsg = null; renderLeave(); } }, "Apply for leave"),
    upcoming.length ? [h("h2", { class: "month-title" }, "Requests"), ...upcoming.map((r) => requestCard(r))] : null,
    past.length ? [h("h2", { class: "month-title" }, "Past"), ...past.slice(0, 10).map((r) => requestCard(r))] : null,
    A ? adminLeaveExtras(A) : null,
    state.xero ? [h("h2", { class: "month-title" }, "Xero"), xeroPanel(state.xero)] : null,
  );
}

function adminLeaveExtras(A) {
  const recent = A.recent.filter((r) => r.status !== "cancelled").slice(0, 15);
  return [
    h("h2", { class: "month-title" }, "Offshore balances"),
    A.balances.length ? A.balances.map((s) => h("article", { class: "leave-req" },
      h("div", { class: "row1" }, h("b", {}, s.name), h("span", { class: "meta" }, s.join_date ? `joined ${fmtDay(s.join_date)}` : "no join date")),
      s.balances ? h("div", {}, s.balances.map((b) => `${b.label}: ${Math.round(b.balance * 10) / 10}`
        + (b.booked ? ` (${Math.round(b.booked * 10) / 10} booked)` : "")).join(" · "))
        : h("div", { class: "meta" }, "Set a join date to start their balance (python -m app.cli set-join-date …)"),
    )) : emptyCard("No offshore staff"),
    recent.length ? [h("h2", { class: "month-title" }, "Recent decisions"), ...recent.map((r) => requestCard(r, { admin: true }))] : null,
  ];
}

// ---------- admin: clock day view ----------

async function loadClockDay(day) {
  state.clockData = await api("/api/admin/clock" + (day ? `?day=${day}` : ""));
  state.clockDay = state.clockData.day;
}

function shiftDays(isoDay, n) {
  const d = parseISO(isoDay); d.setDate(d.getDate() + n); return iso(d);
}

const FLAG_CLASS = { error: "e", warning: "w", info: "i" };

function punchLine(label, p, tz) {
  if (!p) return h("div", { class: "pl missing" }, `${label}: —`);
  const where = p.source === "kiosk" ? `${p.kiosk || "Kiosk"} kiosk`
    : p.source === "admin" ? `entered by ${p.created_by}${p.reason ? ` — “${p.reason}”` : ""}`
      : "phone";
  const map = p.geo && p.geo.status === "ok"
    ? h("a", { href: `https://www.google.com/maps?q=${p.geo.lat},${p.geo.lon}`, target: "_blank", rel: "noopener" },
      `map (±${Math.round(p.geo.accuracy || 0)} m)`)
    : null;
  return h("div", { class: "pl" }, h("b", {}, `${label} ${ABClock.fmtTime(p.occurred_at, tz)}`), ` · ${where}`, map ? " · " : null, map);
}

function localInputValue(isoTs, tz) {
  // "YYYY-MM-DDTHH:MM" in the given zone, for <input type="datetime-local">
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(isoTs)).map((x) => [x.type, x.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function fixForm(key, options, onDone) {
  const err = h("p", { class: "form-error", hidden: true });
  const action = h("select", { "aria-label": "What to fix" }, options.map((o, i) => h("option", { value: String(i) }, o.label)));
  const when = h("input", { type: "datetime-local", "aria-label": "Time (Melbourne)" });
  const reason = h("input", { type: "text", placeholder: "Reason (required), e.g. forgot to clock out", maxlength: "300" });
  const sync = () => {
    const o = options[Number(action.value)];
    when.hidden = !o.needsTime;
    if (o.needsTime && o.defaultTime) when.value = o.defaultTime;
  };
  action.addEventListener("change", sync);
  sync();
  const save = h("button", {
    class: "btn small", onclick: async () => {
      err.hidden = true;
      const o = options[Number(action.value)];
      if (reason.value.trim().length < 3) { err.textContent = "Please give a reason."; err.hidden = false; return; }
      if (o.needsTime && !when.value) { err.textContent = "Please choose a time."; err.hidden = false; return; }
      save.disabled = true;
      try { await o.run(when.value, reason.value.trim()); state.fixOpen = null; await onDone(); }
      catch (ex) { err.textContent = ex.message; err.hidden = false; save.disabled = false; }
    },
  }, "Save");
  return h("div", { class: "fix-form" }, action, when, reason, err,
    h("div", { class: "fix-actions" }, save, h("button", { class: "link-btn dark", onclick: () => { state.fixOpen = null; renderClock(); } }, "Cancel")));
}

const addPunch = (staffId, kind) => (localTime, reason) =>
  api("/api/admin/clock/punch", { method: "POST", body: { staff_id: staffId, kind, local_time: localTime, reason } });
const voidPunch = (id) => (_t, reason) => api("/api/admin/clock/void", { method: "POST", body: { punch_id: id, reason } });

function sessionCard(sess, idx, d) {
  const tz = d.tz;
  const key = `s${idx}`;
  const sh = sess.shift;
  const shiftStart = sh ? `${sh.date}T${sh.start}` : null;
  const nowLocal = localInputValue(new Date().toISOString(), tz);
  // Default a missing clock-out to the rostered end, or now if the shift hasn't finished.
  const shiftEnd = sh ? [`${sh.date}T${sh.end}`, nowLocal].sort()[0] : nowLocal;
  const opts = [];
  if (!sess.clock_out) opts.push({ label: "Add clock-out time", needsTime: true, defaultTime: shiftEnd, run: addPunch(sess.staff_id, "out") });
  if (!sess.clock_in) opts.push({ label: "Add clock-in time", needsTime: true, defaultTime: shiftStart, run: addPunch(sess.staff_id, "in") });
  if (sess.clock_in) opts.push({ label: `Remove clock-in (${ABClock.fmtTime(sess.clock_in.occurred_at, tz)})`, run: voidPunch(sess.clock_in.id) });
  if (sess.clock_out) opts.push({ label: `Remove clock-out (${ABClock.fmtTime(sess.clock_out.occurred_at, tz)})`, run: voidPunch(sess.clock_out.id) });
  const flagged = sess.flags.some((f) => f.level !== "info");
  return h("article", { class: `clock-sess ${flagged ? "flagged" : ""}` },
    h("div", { class: "row1" }, h("b", {}, sess.name), sess.worked_hours != null ? h("span", { class: "hrs" }, `${fmtHours(sess.worked_hours)} worked`) : null),
    punchLine("In", sess.clock_in, tz),
    punchLine("Out", sess.clock_out, tz),
    sh ? h("div", { class: "meta" }, `Rostered ${fmtTime(sh.start)}–${fmtTime(sh.end)} (${fmtHours(sh.scheduled_hours)}) · ${[sh.role, sh.location].filter(Boolean).join(" · ")}`) : null,
    sess.flags.length ? h("div", { class: "flags" }, sess.flags.map((f) => h("span", { class: `flag ${FLAG_CLASS[f.level] || "i"}` }, f.label))) : null,
    state.fixOpen === key ? fixForm(key, opts, reloadClock)
      : h("button", { class: "link-btn dark", onclick: () => { state.fixOpen = key; renderClock(); } }, "Fix…"),
  );
}

function noShowCard(sh, idx, d) {
  const key = `n${idx}`;
  const run = async (_t, reason) => {
    await addPunch(sh.staff_id, "in")(inEl.value, reason);
    if (outEl.value) await addPunch(sh.staff_id, "out")(outEl.value, reason);
  };
  const inEl = h("input", { type: "datetime-local", "aria-label": "Clock-in time (Melbourne)", value: `${sh.date}T${sh.start}` });
  const outEl = h("input", { type: "datetime-local", "aria-label": "Clock-out time (Melbourne)", value: `${sh.date}T${sh.end}` });
  return h("article", { class: "clock-sess flagged" },
    h("div", { class: "row1" }, h("b", {}, sh.name)),
    h("div", { class: "meta" }, `Rostered ${fmtTime(sh.start)}–${fmtTime(sh.end)} · ${[sh.role, sh.location].filter(Boolean).join(" · ")}`),
    h("div", { class: "flags" }, h("span", { class: "flag e" }, "Not clocked in")),
    state.fixOpen === key
      ? h("div", {}, h("p", { class: "hint" }, "If they worked, enter their actual times:"), h("label", {}, "In"), inEl, h("label", {}, "Out (optional)"), outEl,
        fixForm(key, [{ label: "Add these times", run }], reloadClock))
      : h("button", { class: "link-btn dark", onclick: () => { state.fixOpen = key; renderClock(); } }, "They worked — add times…"),
  );
}

async function reloadClock() {
  await loadClockDay(state.clockDay);
  renderClock();
}

function renderClock() {
  const d = state.clockData;
  banner.hidden = true;
  const isToday = d.day === d.today;
  const nav = h("div", { class: "day-nav" },
    h("button", { class: "cal-nav", "aria-label": "Previous day", onclick: async () => { state.fixOpen = null; await loadClockDay(shiftDays(d.day, -1)); renderClock(); } }, "‹"),
    h("div", { class: "day-title" }, fmtDayLong(d.day), isToday ? h("span", { class: "today-pill" }, "Today") : null),
    h("button", { class: "cal-nav", "aria-label": "Next day", disabled: isToday, onclick: async () => { state.fixOpen = null; await loadClockDay(shiftDays(d.day, 1)); renderClock(); } }, "›"),
  );
  const flaggedCount = d.sessions.filter((s) => s.flags.some((f) => f.level !== "info")).length + d.no_shows.length;
  render(
    h("div", { class: "page-head" }, h("h1", {}, "Clock"),
      h("div", { class: "updated" }, h("button", { class: "icon-btn", onclick: reloadClock }, "Refresh"),
        isToday ? null : h("button", { class: "icon-btn", onclick: async () => { await loadClockDay(null); renderClock(); } }, "Today"))),
    nav,
    d.roster_error ? h("p", { class: "form-error" }, `Roster unavailable (${d.roster_error}) — shifts can't be matched right now.`) : null,
    !d.roster_known && !d.roster_error ? h("p", { class: "hint" }, "No roster tab for this month, so punches aren't compared with shifts.") : null,
    d.on_clock.length ? h("section", { class: "on-clock" }, h("h2", { class: "month-title" }, "On the clock now"),
      h("div", { class: "chips" }, d.on_clock.map((o) => h("span", { class: "chip big" }, `${o.name} · since ${ABClock.fmtDayTime(o.since, d.tz)}`)))) : null,
    h("div", { class: "summary" },
      h("span", { class: "ok" }, `${d.sessions.length} clock-in${d.sessions.length === 1 ? "" : "s"}`),
      flaggedCount ? h("span", { class: "w" }, `${flaggedCount} to check`) : null),
    d.no_shows.length ? [h("h2", { class: "month-title" }, "Rostered, not clocked in"), ...d.no_shows.map((sh, i) => noShowCard(sh, i, d))] : null,
    h("h2", { class: "month-title" }, "Clock-ins"),
    d.sessions.length ? d.sessions.map((s, i) => sessionCard(s, i, d)) : emptyCard("No clock-ins on this day"),
  );
}

// ---------- routing ----------

async function route() {
  renderTabbar();
  try {
    if (state.tab === "mine") {
      if (!state.roster) { render(h("p", { class: "loading" }, "Loading your shifts…")); await loadMine(); }
      renderMine();
    } else if (state.tab === "leave") {
      if (!state.leave) { render(h("p", { class: "loading" }, "Loading leave…")); await loadLeave(); }
      renderLeave();
    } else if (state.tab === "clock") {
      if (!state.clockData) { render(h("p", { class: "loading" }, "Loading clock-ins…")); await loadClockDay(state.clockDay); }
      renderClock();
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

const xeroReturn = new URLSearchParams(location.search).get("xero");
if (xeroReturn) history.replaceState(null, "", "/");

async function start() {
  state.roster = null; state.admin = null; state.tab = "mine"; state.clockData = null; state.clockDay = null;
  state.clock = null; state.clockMsg = null;
  state.leave = null; state.leaveAdmin = null; state.leaveFormOpen = false; state.leaveMsg = null;
  if (state.me.must_change_password) return renderChangePassword();
  setChrome(true);
  if (xeroReturn && state.me.role === "admin") {
    state.tab = "leave";
    state.leaveMsg = { connected: "Xero connected. Onshore balances are syncing.", cancelled: "Xero connection was cancelled.",
      failed: "Couldn't connect to Xero. Please try again." }[xeroReturn] || null;
  }
  await route();
  flushMine();
  if (state.me.role === "admin") {
    loadAdmin().then(renderTabbar).catch(() => {});
    api("/api/admin/leave").then((d) => { state.leaveAdmin = d; renderTabbar(); }).catch(() => {});
  }
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
  await refreshPending();
  if (state.pending && !confirm(`${state.pending} clock-in/out ${state.pending === 1 ? "hasn't" : "haven't"} been sent yet (no connection). `
      + "If you log out, it will send next time you log in on this phone. Log out anyway?")) return;
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
    flushMine();
    if (state.tab === "mine") loadMine().then(renderMine).catch(() => {});
  }
});

// Send anything saved offline as soon as we can.
window.addEventListener("online", () => flushMine());
setInterval(() => { if (state.me && navigator.onLine) flushMine(); }, 30000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

boot();
