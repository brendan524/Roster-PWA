"use strict";

/* Shared by the staff app and the kiosk:
   - an outbox (IndexedDB) so a punch is never lost when the connection drops
   - the PIN pad
   - best-effort GPS that never blocks a punch */

window.ABClock = (() => {
  const DB_NAME = "ab-clock";
  const STORE = "outbox";
  const SEND_TIMEOUT_MS = 10000;

  // ---------- outbox ----------

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "client_id" });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function tx(mode, fn) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const t = db.transaction(STORE, mode);
      const result = fn(t.objectStore(STORE));
      t.oncomplete = () => resolve(result && "result" in result ? result.result : undefined);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  }

  const outbox = {
    put: (item) => tx("readwrite", (s) => s.put(item)),
    remove: (id) => tx("readwrite", (s) => s.delete(id)),
    all: () => tx("readonly", (s) => s.getAll()),
  };

  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  async function post(url, body, headers) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), SEND_TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json", "x-ab-app": "1", ...headers },
        body: JSON.stringify({ ...body, sent_at: Date.now() }),
        signal: ctrl.signal,
      });
      let data = null;
      try { data = await res.json(); } catch { /* empty */ }
      return { status: res.status, data };
    } catch {
      return { status: 0, data: null }; // offline, timeout, DNS…
    } finally {
      clearTimeout(timer);
    }
  }

  // Keep it in the outbox and try again later?
  function retryable(status, data) {
    if (status === 0 || status === 429 || status >= 500) return true;
    // Logged out on a personal device: keep it until they log back in.
    return status === 401 && /log in/i.test((data && data.detail) || "");
  }

  /* Submit a punch. Resolves to one of:
       {result: "sent", data}    — stored on the server
       {result: "queued"}        — saved on this device, will send automatically
       {result: "rejected", message, status} — the server said no (e.g. wrong PIN) */
  async function submit(item) {
    // Save first, so even a crash mid-request can't lose it.
    await outbox.put(item);
    const { status, data } = await post(item.url, { ...item.body, queued: false }, item.headers || {});
    if (status >= 200 && status < 300) {
      await outbox.remove(item.client_id);
      return { result: "sent", data };
    }
    if (retryable(status, data)) {
      return { result: "queued" };
    }
    await outbox.remove(item.client_id);
    return { result: "rejected", status, message: (data && data.detail) || "Something went wrong." };
  }

  /* Re-send anything waiting. `filter` picks which items this page may send
     (a personal device only sends the logged-in person's own). */
  let flushing = false;
  async function flush(filter, onRejected) {
    if (flushing) return;
    flushing = true;
    try {
      const items = (await outbox.all()).filter(filter).sort((a, b) => a.body.device_time - b.body.device_time);
      for (const item of items) {
        const { status, data } = await post(item.url, { ...item.body, queued: true }, item.headers || {});
        if (status >= 200 && status < 300) {
          await outbox.remove(item.client_id);
        } else if (retryable(status, data)) {
          break; // still offline (or paused); keep order and try later
        } else {
          await outbox.remove(item.client_id);
          if (onRejected) onRejected(item, (data && data.detail) || "Rejected");
        }
      }
    } finally {
      flushing = false;
    }
  }

  async function pending(filter) {
    try { return (await outbox.all()).filter(filter).length; } catch { return 0; }
  }

  // ---------- GPS ----------

  /* Start getting a location now; resolves within timeoutMs no matter what. */
  function startGeo(timeoutMs = 12000) {
    return new Promise((resolve) => {
      if (!("geolocation" in navigator)) return resolve({ status: "unavailable" });
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      setTimeout(() => finish({ status: "timeout" }), timeoutMs);
      navigator.geolocation.getCurrentPosition(
        (pos) => finish({ status: "ok", lat: pos.coords.latitude, lon: pos.coords.longitude,
                          accuracy: Math.round(pos.coords.accuracy) }),
        (err) => finish({ status: err.code === 1 ? "denied" : err.code === 3 ? "timeout" : "unavailable" }),
        { enableHighAccuracy: true, maximumAge: 60000, timeout: timeoutMs },
      );
    });
  }

  // ---------- PIN pad ----------

  /* Shows a full-screen PIN pad. Resolves with the PIN, or null if cancelled. */
  function pinPad({ title, length = 6 }) {
    return new Promise((resolve) => {
      let pin = "";
      const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
      const overlay = el("div", "pin-overlay");
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      const box = el("div", "pin-box");
      const h = el("h2", "pin-title", title);
      const dots = el("div", "pin-dots");
      dots.setAttribute("aria-live", "polite");
      const grid = el("div", "pin-grid");
      const close = (value) => { document.removeEventListener("keydown", onKey); overlay.remove(); resolve(value); };
      const render = () => {
        dots.replaceChildren(...Array.from({ length }, (_, i) => el("i", i < pin.length ? "on" : "")));
        dots.setAttribute("aria-label", `${pin.length} of ${length} digits entered`);
      };
      const press = (d) => {
        if (pin.length >= length) return;
        pin += d; render();
        if (pin.length === length) setTimeout(() => close(pin), 120);
      };
      const back = () => { pin = pin.slice(0, -1); render(); };
      for (const d of ["1", "2", "3", "4", "5", "6", "7", "8", "9"]) {
        const b = el("button", "pin-key", d); b.type = "button"; b.onclick = () => press(d); grid.append(b);
      }
      const cancel = el("button", "pin-key pin-cancel", "Cancel"); cancel.type = "button"; cancel.onclick = () => close(null);
      const zero = el("button", "pin-key", "0"); zero.type = "button"; zero.onclick = () => press("0");
      const del = el("button", "pin-key pin-del", "⌫"); del.type = "button"; del.setAttribute("aria-label", "Delete"); del.onclick = back;
      grid.append(cancel, zero, del);
      const onKey = (e) => {
        if (/^[0-9]$/.test(e.key)) press(e.key);
        else if (e.key === "Backspace") back();
        else if (e.key === "Escape") close(null);
      };
      document.addEventListener("keydown", onKey);
      box.append(h, dots, grid);
      overlay.append(box);
      document.body.append(overlay);
      render();
      zero.focus();
    });
  }

  function fmtTime(iso, tz) {
    return new Date(iso).toLocaleTimeString("en-AU", { hour: "numeric", minute: "2-digit", timeZone: tz })
      .replace(/\s/g, "").toLowerCase();
  }

  function fmtDayTime(iso, tz) {
    const d = new Date(iso);
    const day = d.toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short", timeZone: tz });
    return `${day}, ${fmtTime(iso, tz)}`;
  }

  return { uuid, submit, flush, pending, startGeo, pinPad, fmtTime, fmtDayTime };
})();
