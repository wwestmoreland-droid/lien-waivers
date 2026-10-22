#!/bin/sh
set -e
lw migrate
# One worker: the scheduler runs in-process. The DB job lock also guards against overlap.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --proxy-headers --forwarded-allow-ips='*'
