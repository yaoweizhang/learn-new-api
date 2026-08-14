#!/usr/bin/env bash
# Quick health check for any chapter. Usage: ./scripts/smoke.sh 8005
set -e
PORT="${1:-8001}"
URL="http://localhost:${PORT}/health"
echo "GET ${URL}"
curl -sS "${URL}" || { echo "FAIL: server not up on :${PORT}"; exit 1; }
echo