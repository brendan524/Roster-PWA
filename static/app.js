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
  clockView: "day",   // day | pay (admin Clock tab)
  pay: null,          // /api/admin/pay
  venues: null,       // /api/admin/venues
  avail: null,        // /api/availability/me
  availDateOpen: false,
  availMsg: null,
  teamView: "roster", // roster | availability (admin Team tab)
  adminAvail: null,   // /api/admin/availability
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

function renderChangePassword(voluntary = false) {
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
        if (voluntary) { state.accountMsg = "Password changed. Other phones you were logged in on have been logged out."; return renderAccount(); }
        await start();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; }
    },
  },
    h("h1", {}, voluntary ? "Change password" : "Set your password"),
    h("p", { class: "sub" }, voluntary ? "Choose a new password." : "Choose a new password to replace the temporary one you were given."),
    h("label", { for: "cur" }, voluntary ? "Current password" : "Temporary password"), cur,
    h("label", { for: "new" }, "New password"), nw,
    h("p", { class: "hint" }, "At least 10 characters."),
    h("label", { for: "again" }, "New password again"), again,
    err, btn,
    voluntary ? h("button", { class: "link-btn dark", type: "button", onclick: () => renderAccount() }, "Cancel") : null,
  );
  render(form);
  cur.focus();
}

function pinCard() {
  const err = h("p", { class: "form-error", hidden: true, role: "alert" });
  const digits = { type: "password", inputmode: "numeric", pattern: "[0-9]*", maxlength: "6", autocomplete: "off" };
  const pin1 = h("input", { ...digits, id: "pin-new" });
  const pin2 = h("input", { ...digits, id: "pin-again" });
  const pw = h("input", { type: "password", id: "pin-pw", autocomplete: "current-password" });
  const save = h("button", { class: "btn", type: "submit" }, "Save my PIN");
  const fail = (msg) => { err.textContent = msg; err.hidden = false; };
  const shown = state.newPin ? h("div", { class: "new-secret", role: "status" },
    h("div", { class: "meta" }, "Your new clock-in PIN"), h("div", { class: "secret" }, state.newPin),
    h("div", { class: "meta" }, "Remember it — it won't be shown again. Your old PIN no longer works.")) : null;
  const random = h("button", { class: "link-btn dark", type: "button", onclick: async () => {
    err.hidden = true;
    if (!pw.value) { fail("Type your password first."); pw.focus(); return; }
    random.disabled = true;
    try {
      const r = await api("/api/auth/new-pin", { method: "POST", body: { password: pw.value } });
      state.newPin = r.pin; state.accountMsg = null; renderAccount(); state.newPin = null;
    } catch (ex) { fail(ex.message); random.disabled = false; }
  } }, "Or let the app pick one for me");
  return h("form", { class: "card account", style: "margin-top:14px", onsubmit: async (e) => {
    e.preventDefault(); err.hidden = true;
    if (!/^[0-9]{6}$/.test(pin1.value)) return fail("Your PIN must be exactly 6 digits.");
    if (pin1.value !== pin2.value) return fail("The two PINs don't match.");
    if (!pw.value) return fail("Type your password to confirm.");
    save.disabled = true;
    try {
      await api("/api/auth/pin", { method: "POST", body: { pin: pin1.value, password: pw.value } });
      state.accountMsg = "PIN saved. Use it next time you clock in."; renderAccount();
    } catch (ex) { fail(ex.message); save.disabled = false; if (ex.status === 409) { pin1.value = ""; pin2.value = ""; pin1.focus(); } }
  } },
    h("b", {}, "Clock-in PIN"),
    h("p", { class: "hint" }, "Choose a new 6-digit PIN. Avoid easy ones like 123456 or your birthday."),
    shown,
    h("label", { for: "pin-new" }, "New PIN"), pin1,
    h("label", { for: "pin-again" }, "New PIN again"), pin2,
    h("label", { for: "pin-pw" }, "Your password (to confirm)"), pw,
    err, save, random);
}

function renderAccount() {
  state.tab = "account";
  renderTabbar();
  banner.hidden = true;
  const me = state.me;
  const err = h("p", { class: "form-error", hidden: true, role: "alert" });
  const email = h("input", { type: "email", id: "acc-email", value: me.email || "", autocomplete: "email",
    autocapitalize: "none", spellcheck: "false", placeholder: "you@example.com" });
  const pw = h("input", { type: "password", id: "acc-pw", autocomplete: "current-password" });
  const save = h("button", { class: "btn", type: "submit" }, "Save email");
  render(
    h("button", { class: "link-btn dark", onclick: () => { state.tab = "mine"; route(); } }, "‹ Back to my shifts"),
    h("div", { class: "page-head" }, h("h1", {}, "My details")),
    state.accountMsg ? h("p", { class: "clock-msg ok", role: "status" }, state.accountMsg) : null,
    h("form", { class: "card account", onsubmit: async (e) => {
      e.preventDefault(); err.hidden = true; save.disabled = true;
      try {
        state.me = await api("/api/auth/email", { method: "POST", body: { email: email.value, password: pw.value } });
        state.accountMsg = state.me.email ? `Saved. Emails will go to ${state.me.email}.` : "Email removed.";
        renderAccount();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; save.disabled = false; }
    } },
      h("div", {}, h("b", {}, me.name), h("span", { class: "meta" }, ` · ${me.staff_id}`)),
      h("label", { for: "acc-email" }, "Email"), email,
      h("p", { class: "hint" }, "We'll email you when your leave is approved or declined. You can also log in with it."),
      h("label", { for: "acc-pw" }, "Your password (to confirm)"), pw,
      err, save,
    ),
    pinCard(),
    h("div", { class: "card account", style: "margin-top:14px" },
      h("b", {}, "Password"),
      h("button", { class: "btn secondary", style: "margin-top:10px", onclick: () => { state.accountMsg = null; renderChangePassword(true); } }, "Change password")),
  );
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
    // Contractors take no leave, so they only have their shifts (no tab bar needed).
    if (state.me.has_leave === false) { tabbar.hidden = true; return; }
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
  const [roster, clock, avail] = await Promise.all([
    api("/api/roster/me" + (refresh ? "?refresh=true" : "")),
    api("/api/clock/me").catch(() => state.clock),
    api("/api/availability/me").catch(() => state.avail),
  ]);
  state.roster = roster;
  state.clock = clock;
  state.avail = avail;
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
    availabilitySection(),
  );
}

// ---------- availability ----------

function fmtWindow(start, end) { return start ? `${fmtTime(start)} – ${fmtTime(end)}` : "all day"; }

function fmtDateRange(a, b) { return a === b ? fmtDay(a) : `${fmtDay(a)} – ${fmtDay(b)}`; }

function entryList(entries, reload) {
  return h("ul", { class: "avail-dates" }, entries.map((d) => h("li", {},
    h("span", {}, h("b", {}, fmtDateRange(d.start_date, d.end_date)), ` · ${fmtWindow(d.start, d.end)}`,
      d.note ? h("span", { class: "meta" }, ` — ${d.note}`) : null),
    h("button", { class: "link-btn small", onclick: async () => {
      if (!confirm("Remove this?")) return;
      try { await api(`/api/availability/me/dates/${d.id}`, { method: "DELETE" }); state.availMsg = "Removed."; await reload(); }
      catch (ex) { alert(ex.message); }
    } }, "Remove"))));
}

function availabilitySection() {
  const A = state.avail;
  if (!A || !A.applies) return null;  // only casual/contract staff; everyone else applies for leave
  const rerender = () => renderMine();
  const reload = async () => { state.avail = await api("/api/availability/me"); rerender(); };
  const monthly = A.mode === "available";
  const clashBox = A.clashes.length ? h("div", { class: "clash" },
    h("b", {}, monthly ? "You're rostered outside the availability you gave:" : "You're rostered at times you said you can't work:"),
    h("ul", { class: "clash-ul" }, A.clashes.slice(0, state.availAllClashes ? 99 : 4).map((c) =>
      h("li", {}, `${fmtDay(c.date)} ${fmtTime(c.start)} – ${fmtTime(c.end)}`))),
    A.clashes.length > 4 && !state.availAllClashes
      ? h("button", { class: "link-btn dark small", onclick: () => { state.availAllClashes = true; renderMine(); } }, `Show all ${A.clashes.length}`) : null,
    h("div", {}, "Please talk to Brendan or Aaron about these.")) : null;
  const msg = state.availMsg ? h("p", { class: "clock-msg ok", role: "status" }, state.availMsg) : null;

  if (!monthly) {
    return h("section", { class: "avail" },
      h("h2", { class: "month-title" }, "My availability"),
      h("p", { class: "hint" }, `Let us know the days or times you can't work, so the roster can be planned around them. Times are ${A.tz_label}.`),
      clashBox, msg,
      h("div", { class: "card avail-card" },
        h("div", { class: "row1" }, h("b", {}, "Days I can't work"),
          state.availDateOpen ? null : h("button", { class: "link-btn dark", onclick: () => { state.availDateOpen = true; state.availMsg = null; rerender(); } }, "Add")),
        state.availDateOpen ? dateEditor({ min: A.today, start: A.today, reload, notePh: "Optional, e.g. away, appointment, uni" }) : null,
        A.dates.length ? entryList(A.dates, reload)
          : (state.availDateOpen ? null : h("div", { class: "meta" }, "None — add any days or times you can't work (e.g. away, appointments, uni).")),
      ));
  }

  const monthCard = (M) => {
    const [y, m] = M.month.split("-").map(Number);
    const first = `${M.month}-01`, last = iso(new Date(y, m, 0));
    const min = first < A.today ? A.today : first;
    const open = state.availDateOpen === M.month;
    const status = M.submitted_at
      ? (M.changed ? h("div", { class: "pay-flags" }, "You've changed this since you submitted — press Submit again to send the update.")
        : h("div", { class: "pay-leave" }, `Submitted ${fmtAgo(M.submitted_at)}${new Date(M.submitted_at).toDateString() === new Date().toDateString() ? "" : ` on ${fmtDay(M.submitted_at.slice(0, 10))}`}.`))
      : (M.due >= A.today ? h("div", { class: "meta" }, `Not submitted yet — due by ${fmtDay(M.due)}.`)
        : h("div", { class: "meta" }, "Not submitted. You can still add times for the rest of this month."));
    const submit = h("button", { class: "btn small", onclick: async () => {
      if (!M.entries.length && !confirm(`You haven't added any times for ${M.label}. Submit as not available at all?`)) return;
      submit.disabled = true;
      try { await api("/api/availability/me/submit", { method: "POST", body: { month: M.month } });
        state.availMsg = `${M.label} submitted. Thanks!`; await reload(); }
      catch (ex) { alert(ex.message); submit.disabled = false; }
    } }, `${M.submitted_at ? "Send update for" : "Submit"} ${M.label.split(" ")[0]}`);
    return h("div", { class: "card avail-card" },
      h("div", { class: "row1" }, h("b", {}, M.label),
        open || last < A.today ? null : h("button", { class: "link-btn dark", onclick: () => { state.availDateOpen = M.month; state.availMsg = null; rerender(); } }, "Add times")),
      status,
      open ? dateEditor({ min, max: last, start: min, reload, notePh: "Optional" }) : null,
      M.entries.length ? entryList(M.entries, reload) : (open ? null : h("div", { class: "meta" }, "No times added yet.")),
      M.submitted_at && !M.changed ? null : h("div", { class: "decide-btns", style: "margin-top:10px" }, submit));
  };
  return h("section", { class: "avail" },
    h("h2", { class: "month-title" }, "My availability"),
    h("p", { class: "hint" }, `Add the days and times you CAN work each month, then press Submit — by the 15th of the month before. Times are ${A.tz_label}.`),
    clashBox, msg,
    A.months.slice().reverse().map(monthCard));
}

function timeInput(value, label) { return h("input", { type: "time", value: value || "", "aria-label": label, step: "900" }); }

function dateEditor({ min, max, start, reload, notePh }) {
  const err = h("p", { class: "form-error", hidden: true, role: "alert" });
  const lim = max ? { max } : {};
  const from = h("input", { type: "date", id: "adf", min, value: start, ...lim });
  const to = h("input", { type: "date", id: "adt", min, value: start, ...lim });
  from.onchange = () => { to.min = from.value; if (to.value < from.value) to.value = from.value; };
  const allDay = h("input", { type: "checkbox", id: "ada", checked: true });
  const t1 = timeInput("", "From time"); const t2 = timeInput("", "To time");
  const times = h("div", { class: "avail-times", hidden: true }, t1, h("span", {}, "to"), t2);
  allDay.onchange = () => { times.hidden = allDay.checked; };
  const note = h("input", { type: "text", id: "adn", maxlength: "300", placeholder: notePh || "Optional" });
  const save = h("button", { class: "btn", type: "submit" }, "Save");
  return h("form", { class: "avail-form", onsubmit: async (e) => {
    e.preventDefault(); err.hidden = true;
    if (!from.value || !to.value) { err.textContent = "Choose the dates."; err.hidden = false; return; }
    if (!allDay.checked && (!t1.value || !t2.value || t1.value >= t2.value)) {
      err.textContent = "Give a from and to time (to after from), or tick All day."; err.hidden = false; return;
    }
    save.disabled = true;
    try {
      await api("/api/availability/me/dates", { method: "POST", body: { start_date: from.value, end_date: to.value,
        start: allDay.checked ? null : t1.value, end: allDay.checked ? null : t2.value, note: note.value } });
      state.availDateOpen = false; state.availMsg = "Saved."; await reload();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; save.disabled = false; }
  } },
  h("div", { class: "date-row" }, h("div", {}, h("label", { for: "adf" }, "From"), from), h("div", {}, h("label", { for: "adt" }, "To"), to)),
  h("label", { class: "check", for: "ada" }, allDay, " All day"),
  times,
  h("label", { for: "adn" }, "Note"), note,
  err, save,
  h("button", { class: "link-btn dark", type: "button", onclick: () => { state.availDateOpen = false; renderMine(); } }, "Cancel"));
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

function teamSwitch() {
  return h("div", { class: "segmented", role: "group", "aria-label": "Show" },
    [["roster", "Roster"], ["availability", "Availability"], ["staff", "Staff"]].map(([v, label]) => h("button", {
      "aria-pressed": state.teamView === v ? "true" : "false",
      onclick: () => { state.teamView = v; renderTeam(); },
    }, label)));
}

async function renderTeamAvailability(reload = false) {
  if (!state.adminAvail || reload) {
    render(teamSwitch(), h("p", { class: "loading" }, "Loading availability…"));
    try { state.adminAvail = await api("/api/admin/availability"); }
    catch (ex) { return render(teamSwitch(), emptyCard("Couldn't load availability", ex.message)); }
  }
  const A = state.adminAvail;
  const people = A.staff.filter((s) => !state.teamStaff || s.staff_id === state.teamStaff);
  render(
    h("div", { class: "page-head" }, h("h1", {}, "Availability"),
      h("div", { class: "updated" }, h("button", { class: "icon-btn", onclick: () => renderTeamAvailability(true) }, "Refresh"))),
    teamSwitch(),
    h("h2", { class: "month-title" }, `Roster clashes — next ${A.days} days`),
    A.clashes.length ? h("div", { class: "clash-list" }, A.clashes.map((c) => h("div", { class: "clash" },
      h("b", {}, `${fmtDay(c.date)} ${fmtTime(c.start)} – ${fmtTime(c.end)} · ${c.name}`),
      h("div", {}, c.reason))))
      : emptyCard("No clashes", "Nobody is rostered while unavailable or on leave."),
    h("h2", { class: "month-title" }, "Casual & contract staff"),
    h("p", { class: "hint" }, "Monthly people give the times they CAN work (due by the 15th); the rest give times they can't. Change who does which under Team → Staff. Everyone else applies for leave."),
    h("select", { "aria-label": "Staff member", onchange: (e) => { state.teamStaff = e.target.value; renderTeamAvailability(); } },
      h("option", { value: "" }, "Everyone"),
      A.staff.map((s) => h("option", { value: s.staff_id, selected: s.staff_id === state.teamStaff }, `${s.name} (${s.staff_id})`))),
    people.map((s) => h("article", { class: "leave-req" },
      h("div", { class: "row1" }, h("b", {}, s.name), h("span", { class: "meta" }, s.mode === "available" ? "Gives monthly availability" : "Gives unavailability")),
      s.mode === "available" ? (s.next_submitted_at
        ? h("div", { class: "pay-leave" }, `${A.next_label}: submitted ${fmtDay(s.next_submitted_at.slice(0, 10))}`)
        : h("div", { class: A.due >= A.today ? "meta" : "pay-flags" }, `${A.next_label}: not submitted (due ${fmtDay(A.due)})`)) : null,
      s.dates.length ? h("ul", { class: "avail-dates" }, s.dates.map((d) => h("li", {},
        h("span", {}, h("b", {}, fmtDateRange(d.start_date, d.end_date)), ` · ${fmtWindow(d.start, d.end)}`,
          d.kind === "available" ? h("span", { class: "meta" }, " · can work") : h("span", { class: "meta" }, " · can't work"),
          d.note ? h("span", { class: "meta" }, ` — ${d.note}`) : null,
          d.new ? h("span", { class: "pill new" }, "New") : null)))) : h("div", { class: "meta" }, "Nothing coming up"),
    )),
  );
}

async function renderStaffAdmin(reload = false) {
  if (!state.staffList || reload) {
    render(teamSwitch(), h("p", { class: "loading" }, "Loading staff…"));
    try { state.staffList = (await api("/api/admin/staff")).staff; }
    catch (ex) { return render(teamSwitch(), emptyCard("Couldn't load staff", ex.message)); }
  }
  const reveal = state.staffReveal;
  const act = (s, kind) => async () => {
    const what = kind === "pin" ? "a new clock-in PIN" : "a new temporary password (they'll be logged out everywhere and must choose a new one)";
    if (!confirm(`Give ${s.name} ${what}?`)) return;
    try {
      const r = await api(`/api/admin/staff/${s.staff_id}/reset-${kind === "pin" ? "pin" : "password"}`, { method: "POST" });
      state.staffReveal = { staff_id: s.staff_id, label: kind === "pin" ? "New PIN" : "Temporary password", value: r.pin || r.password };
      await renderStaffAdmin(true);
    } catch (ex) { alert(ex.message); }
  };
  const q = (state.staffFilter || "").toLowerCase();
  const list = state.staffList.filter((s) => !q || `${s.name} ${s.staff_id}`.toLowerCase().includes(q));
  const search = h("input", { type: "text", placeholder: "Search name or ID", value: state.staffFilter || "", "aria-label": "Search staff",
    oninput: (e) => { state.staffFilter = e.target.value; clearTimeout(state.staffT); state.staffT = setTimeout(() => { renderStaffAdmin().then(() => { const el = document.querySelector("input[aria-label='Search staff']"); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }); }, 250); } });
  render(
    h("div", { class: "page-head" }, h("h1", {}, "Staff"),
      h("div", { class: "updated" }, h("button", { class: "icon-btn", onclick: () => { state.staffReveal = null; renderStaffAdmin(true); } }, "Refresh"))),
    teamSwitch(),
    search,
    list.map((s) => h("article", { class: "pay-card" },
      h("div", { class: "row1" }, h("b", {}, s.name), h("span", { class: "meta" }, s.staff_id)),
      h("div", { class: "meta" }, [s.role === "admin" ? "Admin" : null, s.employment_status, s.location_type,
        s.email || "no email", s.active ? null : "INACTIVE"].filter(Boolean).join(" · ")),
      s.pin_locked || s.login_locked ? h("div", { class: "pay-flags" }, [s.pin_locked ? "PIN locked (too many wrong tries)" : null,
        s.login_locked ? "Login locked (too many wrong passwords)" : null].filter(Boolean).join(" · ")) : null,
      !s.has_pin ? h("div", { class: "pay-flags" }, "No PIN set") : null,
      s.location_type === "onshore" && s.role !== "admin" ? h("label", { class: "check" },
        h("input", { type: "checkbox", checked: s.travel_eligible, onchange: async (e) => {
          try { await api(`/api/admin/staff/${s.staff_id}/travel`, { method: "POST", body: { on: e.target.checked } }); s.travel_eligible = e.target.checked; state.pay = null; }
          catch (ex) { alert(ex.message); e.target.checked = !e.target.checked; }
        } }), " First Aid travel paid (time + km to client sites)") : null,
      s.availability ? h("label", { class: "inline-select" }, "Availability: ",
        h("select", { onchange: async (e) => {
          try { await api(`/api/admin/staff/${s.staff_id}/availability-mode`, { method: "POST", body: { mode: e.target.value } });
            s.availability = e.target.value; state.adminAvail = null; }
          catch (ex) { alert(ex.message); e.target.value = s.availability; }
        } },
          h("option", { value: "unavailable", selected: s.availability === "unavailable" }, "Tells us when they can't work"),
          h("option", { value: "available", selected: s.availability === "available" }, "Submits monthly availability"))) : null,
      reveal && reveal.staff_id === s.staff_id ? h("div", { class: "new-secret", role: "status" },
        h("div", { class: "meta" }, `${reveal.label} for ${s.name}`), h("div", { class: "secret" }, reveal.value),
        h("div", { class: "meta" }, "Give it to them privately. It won't be shown again.")) : null,
      h("div", { class: "decide-btns", style: "margin-top:8px" },
        h("button", { class: "btn small secondary", onclick: act(s, "pin") }, "Reset PIN"),
        s.staff_id === state.me.staff_id ? null : h("button", { class: "btn small secondary", onclick: act(s, "password") }, "Reset password")),
    )),
  );
}

function renderTeam() {
  if (state.teamView === "staff") return renderStaffAdmin();
  if (state.teamView === "availability") return renderTeamAvailability();
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
    teamSwitch(),
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
          : h("div", { class: "meta" }, !st.link ? "Not linked — pick their Xero name"
            : st.link.casual ? "Casual in Xero — no paid leave" : "No balances in Xero"),
        st.link && st.link.start_date ? h("div", { class: "meta" }, `Started ${parseISO(st.link.start_date).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" })} (long service shows to them after 7 years)`) : null),
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

const MAX_FILE_MB = 10;

async function uploadFile(requestId, file) {
  if (file.size > MAX_FILE_MB * 1024 * 1024) throw new Error(`${file.name} is over ${MAX_FILE_MB} MB.`);
  let res;
  try {
    res = await fetch(`/api/leave/requests/${requestId}/attachments`, {
      method: "POST", credentials: "same-origin", body: file,
      headers: { "x-ab-app": "1", "x-filename": encodeURIComponent(file.name), "content-type": "application/octet-stream" },
    });
  } catch { throw new Error("No connection — the file wasn't uploaded."); }
  if (!res.ok) {
    let msg = "Upload failed.";
    try { msg = (await res.json()).detail || msg; } catch { /* empty */ }
    throw new Error(`${file.name}: ${msg}`);
  }
}

function fmtSize(n) { return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`; }

function attachmentList(r, { canRemove = false } = {}) {
  if (!r.attachments || !r.attachments.length) return null;
  return h("ul", { class: "files" }, r.attachments.map((a) => h("li", {},
    h("a", { href: `/api/leave/attachments/${a.id}`, target: "_blank", rel: "noopener" }, `📎 ${a.filename}`),
    h("span", { class: "meta" }, ` ${fmtSize(a.size)}`),
    canRemove ? h("button", { class: "link-btn dark small", "aria-label": `Remove ${a.filename}`, onclick: async () => {
      if (!confirm(`Remove ${a.filename}?`)) return;
      try { await api(`/api/leave/attachments/${a.id}`, { method: "DELETE" }); await loadLeave(); renderLeave(); }
      catch (ex) { alert(ex.message); }
    } }, "Remove") : null,
  )));
}

function addFileButton(r) {
  const input = h("input", { type: "file", accept: "application/pdf,image/*", multiple: true, hidden: true });
  input.addEventListener("change", async () => {
    const errors = [];
    for (const f of input.files) { try { await uploadFile(r.id, f); } catch (ex) { errors.push(ex.message); } }
    if (errors.length) alert(errors.join("\n"));
    await loadLeave(); renderLeave();
  });
  return h("span", {}, input, h("button", { class: "link-btn dark", onclick: () => input.click() }, "Add a file"));
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
  const reason = h("input", { type: "text", id: "lr", maxlength: "500", "aria-required": "true",
    placeholder: "e.g. family holiday, sick with flu, caring for my child" });
  const files = h("input", { type: "file", id: "lf", accept: "application/pdf,image/*", multiple: true });
  reason.addEventListener("input", () => { if (reason.value.trim().length >= 3) err.hidden = true; });
  const typeHint = h("p", { class: "hint" });
  const preview = h("p", { class: "leave-preview" });
  const ehWrap = h("label", { class: "check", for: "leh" }, eh, " Last day is a half day");
  const notice = L.notice_days || 7;
  const earliest = shiftDays(L.today, notice);
  const update = () => {
    const t = L.types.find((x) => x.code === type.value);
    start.min = t.advance_only ? earliest : shiftDays(L.today, -(L.max_backdate_days || 60));
    typeHint.textContent = t.advance_only ? `Needs at least ${notice} days' notice — earliest start ${fmtDay(earliest)}.`
      : "Can be for today or past dates (up to 60 days back), e.g. if you were sick.";
    if (start.value && start.value < start.min) start.value = "";
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
  update();
  const btn = h("button", { class: "btn", type: "submit" }, "Send request");
  return h("form", {
    class: "card leave-form", onsubmit: async (e) => {
      e.preventDefault(); err.hidden = true;
      if (reason.value.trim().length < 3) { err.textContent = "Please give a reason for your leave."; err.hidden = false; reason.focus(); return; }
      const big = [...files.files].find((f) => f.size > MAX_FILE_MB * 1024 * 1024);
      if (big) { err.textContent = `${big.name} is over ${MAX_FILE_MB} MB.`; err.hidden = false; return; }
      btn.disabled = true;
      try {
        const created = await api("/api/leave/requests", { method: "POST", body: {
          leave_type: type.value, start_date: start.value, end_date: end.value,
          start_half: sh.checked, end_half: !ehWrap.hidden && eh.checked, reason: reason.value,
        } });
        const failed = [];
        for (const f of files.files) { try { await uploadFile(created.id, f); } catch (ex) { failed.push(ex.message); } }
        state.leaveFormOpen = false;
        state.leaveMsg = failed.length
          ? `Request sent, but a file didn't upload (${failed.join("; ")}). Use "Add a file" on your request below to try again.`
          : "Request sent. You'll see it here once it's approved.";
        await loadLeave(); renderTabbar(); renderLeave();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; }
    },
  },
    h("label", { for: "lt" }, "Type of leave"), type, typeHint,
    h("div", { class: "date-row" },
      h("div", {}, h("label", { for: "ls" }, "From"), start),
      h("div", {}, h("label", { for: "le" }, "To"), end)),
    h("label", { class: "check", for: "lsh" }, sh, " First day is a half day"),
    ehWrap,
    h("label", { for: "lr" }, "Reason"), reason,
    h("label", { for: "lf" }, "Attachments (optional)"), files,
    h("p", { class: "hint" }, "e.g. a medical certificate. PDF or photo, up to 10 MB each. You can also add files later."),
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
    attachmentList(r, { canRemove: !admin && r.status === "pending" }),
    !admin && (r.status === "pending" || r.status === "approved") && (r.attachments || []).length < 5 ? addFileButton(r) : null,
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
  if (L.has_leave === false) balanceBlock = h("p", { class: "hint" }, "Contractors don't take leave through the app.");
  else if (L.casual) balanceBlock = h("p", { class: "hint" }, "Casual staff don't have paid leave, but you can still ask for unpaid time off.");
  else if (L.balances) balanceBlock = balanceCards(L.balances);
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
    !state.me.email && L.has_leave !== false ? h("p", { class: "hint" }, "Want an email when your leave is approved? ",
      h("button", { class: "link-btn dark small", onclick: () => { state.accountMsg = null; renderAccount(); } }, "Add your email")) : null,
    state.leaveMsg ? h("p", { class: "clock-msg ok", role: "status" }, state.leaveMsg) : null,
    L.has_leave === false ? null : state.leaveFormOpen ? leaveForm(L)
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
    : p.source === "auto" ? "automatic (rostered time)"
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

// ---------- pay report (admin) ----------

const EMP_LABEL = { casual: "Casual", "part-time": "Part-time", "full-time": "Full-time", contractor: "Contractor",
  salaried: "Salaried (offshore)", unknown: "Employment type unknown" };

function fmtLongDate(s) { return parseISO(s).toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short", year: "numeric" }); }

async function renderPay(end, reload = false) {
  const head = (extra) => [h("div", { class: "page-head" }, h("h1", {}, "Pay report"), extra || null), clockSwitch()];
  if (!state.pay || reload || (end && state.pay.period_end !== end)) {
    render(head(), h("p", { class: "loading" }, "Working out the pay report…"));
    try {
      [state.pay, state.venues] = await Promise.all([
        api("/api/admin/pay" + (end ? `?end=${end}` : (state.pay ? `?end=${state.pay.period_end}` : ""))),
        api("/api/admin/venues"),
      ]);
    } catch (ex) { return render(head(), emptyCard("Couldn't make the pay report", ex.message)); }
  }
  const R = state.pay;
  const open = state.payOpen || (state.payOpen = new Set());
  const periodSel = h("select", { "aria-label": "Pay period", onchange: (e) => renderPay(e.target.value) },
    R.periods.map((p) => h("option", { value: p.end, selected: p.end === R.period_end },
      `${fmtDay(p.start)} – ${fmtDay(p.end)}${p.end === R.periods[0].end ? " (current)" : ""}`)));
  const checks = R.people.reduce((n, p) => n + p.flags.length, 0);
  const groups = [
    ["Enter in Xero", (p) => ["casual", "part-time", "full-time", "unknown"].includes(p.employment)],
    ["Contractors — check against invoices", (p) => p.employment === "contractor"],
    ["Offshore — salaried", (p) => p.employment === "salaried"],
  ];
  const card = (p) => h("article", { class: "pay-card" },
    h("div", { class: "row1" }, h("b", {}, p.name), h("span", { class: "meta" }, [EMP_LABEL[p.employment] || p.employment, ...p.teams].join(" · "))),
    p.employment === "contractor"
      ? h("div", { class: "meta" }, `Clocked ${p.shifts.reduce((n, s) => n + (s.clocked_hours || 0), 0)} h · rostered ${p.shifts.reduce((n, s) => n + s.rostered_hours, 0)} h`)
      : (p.items.length ? h("table", { class: "pay-items" }, p.items.map((it) => h("tr", {}, h("td", {}, it.item), h("td", {}, String(it.qty)))))
        : h("div", { class: "meta" }, "Nothing to pay this period")),
    p.leave.map((l) => h("div", { class: "pay-leave" }, `Leave: ${l}`)),
    p.flags.length ? h("ul", { class: "pay-flags" }, p.flags.map((f) => h("li", {}, f))) : null,
    p.shifts.length ? h("button", { class: "link-btn dark small", onclick: () => { open.has(p.staff_id) ? open.delete(p.staff_id) : open.add(p.staff_id); renderPay(); } },
      open.has(p.staff_id) ? "Hide shifts" : `Show ${p.shifts.length} shift${p.shifts.length === 1 ? "" : "s"}`) : null,
    open.has(p.staff_id) ? h("table", { class: "pay-shifts" },
      h("tr", {}, h("th", {}, "Day"), h("th", {}, "Rostered"), h("th", {}, "Clocked"), h("th", {}, "Paid")),
      p.shifts.flatMap((s) => [
        h("tr", {}, h("td", {}, fmtDay(s.date)), h("td", {}, `${s.start}–${s.end}`),
          h("td", {}, s.clock_in || s.clock_out ? `${s.clock_in || "?"}–${s.clock_out || "?"}` : "—"),
          h("td", {}, s.teaching_rostered != null ? `${s.teaching_rostered} teach` : (s.paid_hours != null ? `${s.paid_hours} h` : ""))),
        s.notes.length || s.travel ? h("tr", { class: "sub" }, h("td", { colspan: "4" },
          [s.location, s.travel ? `${s.travel.km} km, ${s.travel.hours} h travel` : "", ...s.notes].filter(Boolean).join(" · "))) : null,
      ].filter(Boolean))) : null,
  );
  render(
    head(h("div", { class: "updated" }, h("button", { class: "icon-btn", onclick: () => renderPay(R.period_end, true) }, "Refresh"))),
    periodSel,
    h("p", { class: "hint" }, `${fmtLongDate(R.period_start)} – ${fmtLongDate(R.period_end)} · pay day ${fmtLongDate(R.pay_day)}`
      + (R.emailed_at ? ` · emailed ${fmtAgo(R.emailed_at)}` : R.current ? " · still in progress" : "")),
    h("div", { class: "summary" }, checks ? h("span", { class: "w" }, `${checks} to check`) : h("span", { class: "ok" }, "Nothing flagged"),
      h("button", { class: "icon-btn", onclick: async (e) => {
        if (!confirm(`Email this report to ${R.email_to || "(no address set)"}?`)) return;
        e.target.disabled = true;
        try { await api(`/api/admin/pay/email?end=${R.period_end}`, { method: "POST" }); alert("Sent."); }
        catch (ex) { alert(ex.message); } finally { e.target.disabled = false; }
      } }, "Email it")),
    groups.map(([title, test]) => {
      const ppl = R.people.filter(test);
      return ppl.length ? [h("h2", { class: "month-title" }, title), ppl.map(card)] : null;
    }),
    R.people.length ? null : emptyCard("No shifts in this period"),
    R.tracker.length ? [h("h2", { class: "month-title" }, "First Aid teaching hours"), teachingTracker(R)] : null,
    venuesPanel(),
  );
}

function teachingTracker(R) {
  return h("div", { class: "card pay-card" },
    h("p", { class: "hint" }, "Progression every 800 teaching hours. Totals include this period."),
    R.tracker.map((t) => h("div", { class: "tracker-row" },
      h("div", {}, h("b", {}, t.name), t.reached ? h("span", { class: "pill new" }, `Reached ${t.reached.toLocaleString()} h`) : null),
      h("div", { class: "meta" }, `${t.total.toLocaleString()} h · Level ${t.level} · next level at ${t.next_milestone.toLocaleString()} h`),
      h("button", { class: "link-btn dark small", onclick: async () => {
        const hrs = prompt(`${t.name}: teaching hours delivered up to the end of the last pay period before the app (e.g. 1650)`, String(t.opening || ""));
        if (hrs === null) return;
        const asOf = prompt("Up to which date? (YYYY-MM-DD)", t.opening_as_of || "2026-10-04");
        if (!asOf) return;
        try { await api("/api/admin/teaching-hours", { method: "POST", body: { staff_id: t.staff_id, hours: Number(hrs), as_of: asOf } }); renderPay(R.period_end, true); }
        catch (ex) { alert(ex.message); }
      } }, "Set starting hours"))));
}

function venuesPanel() {
  const V = state.venues;
  if (!V || !V.venues.length) return null;
  const unknown = V.venues.filter((v) => v.kind === "unknown").length;
  return [h("h2", { class: "month-title" }, `First Aid venues${unknown ? ` (${unknown} to set up)` : ""}`),
    h("p", { class: "hint" }, "Campus = gear kept there, no travel paid. Client site = travel time and km paid from the office"
      + (V.office_address ? ` (${V.office_address}).` : " — set OFFICE_ADDRESS on the server first.")
      + (V.maps ? " Distances are looked up on Google Maps." : " Type the one-way km and minutes from the office.")),
    V.venues.map((v) => {
      const kind = h("select", { "aria-label": `${v.label} type` },
        [["unknown", "Not set"], ["campus", "Campus"], ["client", "Client site"]].map(([k, l]) => h("option", { value: k, selected: v.kind === k }, l)));
      const addr = h("input", { type: "text", value: v.address, placeholder: "Street address (for client sites)", "aria-label": `${v.label} address` });
      const km = h("input", { type: "text", inputmode: "decimal", value: v.km ?? "", placeholder: "km", "aria-label": `${v.label} km one way` });
      const mins = h("input", { type: "text", inputmode: "numeric", value: v.minutes ?? "", placeholder: "min", "aria-label": `${v.label} minutes one way` });
      const save = h("button", { class: "btn small", onclick: async () => {
        save.disabled = true;
        const manual = !V.maps && km.value && mins.value;
        try {
          await api("/api/admin/venues", { method: "POST", body: { name: v.label, kind: kind.value, address: addr.value,
            km: manual ? Number(km.value) : null, minutes: manual ? Number(mins.value) : null } });
          state.venues = await api("/api/admin/venues"); renderPay(state.pay.period_end, true);
        } catch (ex) { alert(ex.message); save.disabled = false; }
      } }, "Save");
      return h("div", { class: "card venue" },
        h("b", {}, v.label), kind, addr,
        V.maps ? (v.km != null ? h("div", { class: "meta" }, `${v.km} km, ${Math.round(v.minutes)} min each way from the office`) : null)
          : h("div", { class: "venue-km" }, km, mins, h("span", { class: "meta" }, "one way")),
        save);
    })];
}

function clockSwitch() {
  return h("div", { class: "segmented", role: "group", "aria-label": "Show" },
    [["day", "Clock-ins"], ["pay", "Pay report"]].map(([v, label]) => h("button", {
      "aria-pressed": (state.clockView || "day") === v ? "true" : "false",
      onclick: () => { state.clockView = v; renderClock(); },
    }, label)));
}

function renderClock() {
  if (state.clockView === "pay") return renderPay();
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
    clockSwitch(),
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
    } else if (state.tab === "account") {
      renderAccount();
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
  state.avail = null; state.adminAvail = null; state.availDateOpen = false; state.availMsg = null; state.availAllClashes = false;
  state.staffList = null; state.staffReveal = null; state.newPin = null; state.pay = null; state.venues = null;
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

document.getElementById("username").addEventListener("click", () => {
  if (!state.me || state.me.must_change_password) return;
  state.accountMsg = null;
  renderAccount();
});

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
  navigator.serviceWorker.register("/sw.js").then((reg) => {
    // Installed apps are often resumed rather than restarted: look for a new version each time they come back.
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") reg.update().catch(() => {}); });
  }).catch(() => {});
  // A new version took over: reload to use it, unless the person is typing (then on their next return).
  let hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!hadController) { hadController = true; return; }  // first install, nothing old running
    const typing = () => document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
    if (!typing()) return location.reload();
    document.addEventListener("visibilitychange", function once() {
      if (document.visibilityState === "visible") { document.removeEventListener("visibilitychange", once); location.reload(); }
    });
  });
}

boot();
