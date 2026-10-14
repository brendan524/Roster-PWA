#!/bin/sh
set -e
# Apply any pending database migrations, then start the app.
alembic upgrade head
# One worker: the roster cache lives in memory, and this load is tiny.
# --proxy-headers: trust X-Forwarded-* from Traefik (only Traefik can reach us).
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips='*'
