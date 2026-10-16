import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import autoclock, jobs
from app import xero as xero_sync
from app.api import auth, availability, clock, expenses, leave, pay, roster, staff_admin, xero
from app.config import get_settings
from app.db import SessionLocal, engine
from app.deps import get_roster_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="AB Group", docs_url=None, redoc_url=None, openapi_url=None)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
CSRF_HEADER = "x-ab-app"
# Always re-checked with the server (ETag), so phones never run an old copy of the app.
NO_CACHE_FILES = {"/", "/index.html", "/sw.js", "/manifest.webmanifest", "/kiosk.html", "/kiosk.webmanifest",
                  "/app.js", "/app.css", "/clock-shared.js", "/kiosk.js", "/kiosk.css"}


@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path
    # CSRF guard: browsers won't send a custom header cross-site without a CORS
    # preflight, which we never grant. So any state-changing API call must carry it.
    if path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Missing app header."}, status_code=403)

    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)  # attachments set a stricter one
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif path in NO_CACHE_FILES:
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(auth.router)
app.include_router(roster.router)
app.include_router(clock.router)
app.include_router(leave.router)
app.include_router(xero.router)
app.include_router(availability.router)
app.include_router(pay.router)
app.include_router(staff_admin.router)
app.include_router(expenses.router)


@app.on_event("startup")
def _start_background_jobs():
    xero_sync.start_background_sync(SessionLocal, get_settings().xero_sync_hours)
    autoclock.start_background(SessionLocal, get_roster_service, get_settings().auto_clock_minutes)
    jobs.start_background(SessionLocal, get_roster_service)


@app.get("/kiosk", include_in_schema=False)
def kiosk_page():
    return RedirectResponse("/kiosk.html")


@app.get("/api/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
