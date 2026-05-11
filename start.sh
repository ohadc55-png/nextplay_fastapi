#!/bin/sh
# Process selector — picks which uvicorn app to run based on SERVICE_TYPE env var.
#
# - SERVICE_TYPE=frontend  -> Starlette static-only (frontend_server.py)
# - anything else / unset  -> FastAPI backend (src.main:app) with alembic migrations
#
# Set SERVICE_TYPE=frontend on the Railway "nextplay-frontend" service.
# Leave it unset (default) on the backend service.

set -e

# Always run the asset minifier — both services may serve frontend/static/.
python scripts/build.py

if [ "$SERVICE_TYPE" = "frontend" ]; then
    echo "[start.sh] Booting FRONTEND (Starlette static-only)"
    exec uvicorn frontend_server:app \
        --host 0.0.0.0 \
        --port "$PORT" \
        --workers 2 \
        --access-log
fi

echo "[start.sh] Booting BACKEND (FastAPI)"
alembic upgrade head
exec uvicorn src.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 2 \
    --timeout-keep-alive 300 \
    --access-log
