#!/usr/bin/env bash
# Start Ringside. `./run.sh --public` also opens a Cloudflare quick tunnel: an https URL for the judge's phone
# (browsers only open a microphone on https or localhost) and for Twilio's webhook.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
PORT="${PORT:-8080}"
mkdir -p logs
if [ "${1:-}" = "--public" ]; then
  : > logs/tunnel.log
  # shellcheck disable=SC2086
  cloudflared tunnel --url "http://localhost:$PORT" ${TUNNEL_ARGS:-} > logs/tunnel.log 2>&1 &
  for _ in $(seq 1 40); do
    HOST=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log | head -1 | sed 's#https://##' || true)
    [ -n "$HOST" ] && break
    sleep 0.5
  done
  if [ -n "${HOST:-}" ]; then export PUBLIC_HOST="$HOST"; echo "public: https://$HOST  (judge seat + Twilio webhook https://$HOST/twilio/voice)"; else echo "tunnel did not come up; see logs/tunnel.log"; fi
fi
echo "local:  http://localhost:$PORT"
exec .venv/bin/uvicorn app.server:app --host 0.0.0.0 --port "$PORT"
