#!/bin/sh
set -e

# The volume is mounted root-owned, so ownership is fixed here rather than at
# build time -- at build time /data does not exist yet.
mkdir -p "${SARGAM_DATA:-/data/users}"
chown -R app:app /data 2>/dev/null || true

# One worker, deliberately. Each worker would hold its own registry of open
# stores, and two of them writing the same user's SQLite file is a corruption
# waiting to happen. Concurrency here is per-user locks inside one process,
# not processes.
exec setpriv --reuid=app --regid=app --clear-groups \
  uvicorn sargam.server:app \
    --host 0.0.0.0 \
    --port "${PORT:-8080}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips='*'
