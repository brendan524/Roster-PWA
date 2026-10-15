"use strict";

/* AB Group kiosk: a shared device at an office. Press Clock in / Clock out, enter PIN.
   Punches are saved on the device first and sent when there's a connection. */

const root = document.getElementById("kiosk");
const TOKEN_KEY = "ab.kiosk";
const RESULT_MS = 4500;

let kiosk = null; // {token, name, location_type, tz}
let clockTimer = null;
let serverOffsetMs = 0; // server clock minus this device's clock, for the on-screen clock only

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function loadKiosk() { try { return JSON.parse(localStorage.getItem(TOKEN_KEY)); } catch { return null; } }
function saveKiosk(k) { try { localStorage.setItem(TOKEN_KEY, JSON.stringify(k)); } catch { /* private mode */ } }
function clearKiosk() { try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } }

const kioskFilter = (item) => item.url === "/api/kiosk/punch" && kiosk && item.headers && item.headers["x-kiosk-token"] === kiosk.token;

// ---------- setup ----------

async function renderSetup(message) {
  stopClock();
  let locations = [];
  try { locations = await (await fetch("/api/kiosk/locations")).json(); } catch { /* offline */ }
  const err = h("p", { class: "form-error", hidden: !message, role: "alert" }, message || "");
  const loc = h("select", { id: "loc", required: true }, locations.map((l) => h("option", { value: l.name }, l.name)));
  const id = h("input", { type: "text", id: "id", autocomplete: "username", required: true, autocapitalize: "none", placeholder: "Admin Staff ID" });
  const pw = h("input", { type: "password", id: "pw", autocomplete: "current-password", required: true });
  const btn = h("button", { class: "btn", type: "submit" }, "Set up this kiosk");
  root.replaceChildren(
    h("img", { class: "k-logo", src: "/logo.png", alt: "The AB Group" }),
    h("form", {
      class: "card k-setup", onsubmit: async (e) => {
        e.preventDefault(); err.hidden = true; btn.disabled = true;
        try {
          const res = await fetch("/api/kiosk/setup", {
            method: "POST", headers: { "content-type": "application/json", "x-ab-app": "1" },
            body: JSON.stringify({ login: id.value, password: pw.value, location: loc.value }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || "Setup failed.");
          kiosk = data; saveKiosk(data); start();
        } catch (ex) {
          err.textContent = ex.message === "Failed to fetch" ? "No connection. Setup needs the internet." : ex.message;
          err.hidden = false; btn.disabled = false; pw.value = "";
        }
      },
    },
    h("h1", {}, "Set up kiosk"),
    h("p", { class: "sub" }, "An admin signs in once to turn this device into a clock-in kiosk. Staff then only need their PIN."),
    h("label", { for: "loc" }, "Location"), loc,
    h("label", { for: "id" }, "Admin Staff ID"), id,
    h("label", { for: "pw" }, "Password"), pw,
    err, btn),
  );
}

// ---------- main screen ----------

function nowServer() { return new Date(Date.now() + serverOffsetMs); }

function stopClock() { if (clockTimer) clearInterval(clockTimer); clockTimer = null; }

async function statusBar() {
  const n = await ABClock.pending(kioskFilter);
  const online = navigator.onLine;
  return h("div", { class: "k-status" },
    h("span", {}, kiosk.name),
    n ? h("span", { class: "bad" }, `${n} waiting to send${online ? "" : " · offline"}`)
      : h("span", { class: online ? "" : "bad" }, online ? "Online" : "Offline — punches are saved here"),
  );
}

async function renderHome() {
  const time = h("div", { class: "k-time" });
  const date = h("div", { class: "k-date" });
  const tick = () => {
    const d = nowServer();
    time.textContent = d.toLocaleTimeString("en-AU", { hour: "numeric", minute: "2-digit", timeZone: kiosk.tz });
    date.textContent = d.toLocaleDateString("en-AU", { weekday: "long", day: "numeric", month: "long", timeZone: kiosk.tz });
  };
  tick();
  stopClock();
  clockTimer = setInterval(tick, 1000);
  root.replaceChildren(
    h("img", { class: "k-logo", src: "/logo.png", alt: "The AB Group" }),
    h("div", { class: "k-place" }, kiosk.name),
    time, date,
    h("div", { class: "k-buttons" },
      h("button", { class: "k-btn in", onclick: () => punch("in") }, "Clock in"),
      h("button", { class: "k-btn out", onclick: () => punch("out") }, "Clock out"),
    ),
    await statusBar(),
  );
}

function showResult(kind, icon, big, sub) {
  stopClock();
  root.replaceChildren(h("div", { class: `k-result ${kind}` },
    h("div", { class: "tick", "aria-hidden": "true" }, icon),
    h("div", { class: "big" }, big),
    sub ? h("div", { class: "sub" }, sub) : null,
  ));
  setTimeout(renderHome, RESULT_MS);
}

async function punch(kind) {
  stopClock();
  const pin = await ABClock.pinPad({ title: kind === "in" ? "Enter your PIN to clock in" : "Enter your PIN to clock out" });
  if (!pin) return renderHome();
  const pressedAt = Date.now();
  root.replaceChildren(h("p", { class: "loading" }, "Saving…"));
  const client_id = ABClock.uuid();
  const res = await ABClock.submit({
    client_id, url: "/api/kiosk/punch", headers: { "x-kiosk-token": kiosk.token },
    body: { client_id, kind, pin, device_time: pressedAt, geo: { status: "unavailable" } },
  });
  const word = kind === "in" ? "in" : "out";
  if (res.result === "sent") {
    const d = res.data;
    const t = ABClock.fmtTime(d.occurred_at, kiosk.tz);
    showResult("ok", "✓", `Thanks, ${d.name}`, d.duplicate ? `Already clocked ${word} at ${t}` : `Clocked ${word} at ${t}`);
  } else if (res.result === "queued") {
    showResult("warn", "✓", "Saved", `No internet right now — your clock-${word} at ${ABClock.fmtTime(new Date(pressedAt).toISOString(), kiosk.tz)} is stored here and will send automatically.`);
  } else if (res.status === 401 && /kiosk/i.test(res.message)) {
    clearKiosk(); kiosk = null; renderSetup(res.message);
  } else {
    showResult("err", "✕", res.message, "Please try again.");
  }
}

// ---------- sync + keep awake ----------

async function flushKiosk() {
  if (!kiosk) return;
  await ABClock.flush(kioskFilter, () => { /* rejected (e.g. wrong PIN) — the person already saw "Saved"; it's dropped */ });
}

let wakeLock = null;
async function keepAwake() {
  try { if ("wakeLock" in navigator && document.visibilityState === "visible") wakeLock = await navigator.wakeLock.request("screen"); } catch { /* not supported */ }
}

async function checkIn() {
  // Confirm the kiosk is still registered, and learn the server's clock.
  try {
    const before = Date.now();
    const res = await fetch("/api/kiosk/me", { headers: { "x-kiosk-token": kiosk.token } });
    if (res.status === 401) { clearKiosk(); kiosk = null; return renderSetup("This kiosk was removed. An admin needs to set it up again."); }
    if (res.ok) {
      const d = await res.json();
      serverOffsetMs = new Date(d.server_time).getTime() - (before + Date.now()) / 2;
      Object.assign(kiosk, { name: d.name, tz: d.tz, location_type: d.location_type });
      saveKiosk(kiosk);
    }
  } catch { /* offline: keep going with what we have */ }
}

async function start() {
  kiosk = loadKiosk();
  if (!kiosk || !kiosk.token) return renderSetup();
  await renderHome();
  await checkIn();
  if (kiosk) { await flushKiosk(); renderHome(); }
}

window.addEventListener("online", async () => { await flushKiosk(); if (kiosk && clockTimer) renderHome(); });
setInterval(async () => { if (kiosk && navigator.onLine) { await flushKiosk(); } }, 30000);
setInterval(() => { if (kiosk) checkIn(); }, 10 * 60 * 1000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") keepAwake(); });
keepAwake();

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
start();
