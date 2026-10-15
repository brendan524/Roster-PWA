import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import auth, clock, roster
from app.db import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="AB Group", docs_url=None, redoc_url=None, openapi_url=None)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
CSRF_HEADER = "x-ab-app"
NO_CACHE_FILES = {"/", "/index.html", "/sw.js", "/manifest.webmanifest"}


@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path
    # CSRF guard: browsers won't send a custom header cross-site without a CORS
    # preflight, which we never grant. So any state-changing API call must carry it.
    if path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Missing app header."}, status_code=403)

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
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


@app.get("/api/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
