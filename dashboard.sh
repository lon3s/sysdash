#!/usr/bin/env bash
# sysdash launcher — startet NUR den Server (127.0.0.1:8787).
# Firefox öffnest du selbst: http://127.0.0.1:8787/
# und ziehst das Fenster einmalig auf das Acer-Display.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${SYSDASH_PORT:-8787}"

# Bereits am Laufen?
if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  echo "SYSDASH-Server läuft bereits auf Port ${PORT}."
  echo "Öffne im Browser: http://127.0.0.1:${PORT}/"
  exit 0
fi

echo "Starte SYSDASH-Server auf Port ${PORT} …"
nohup "${DIR}/.venv/bin/python" "${DIR}/main.py" >> "${DIR}/server.log" 2>&1 &
echo $! > "${DIR}/server.pid"

# Warten bis der Server antwortet
for _ in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "Server läuft (PID $(cat "${DIR}/server.pid"))."
    echo "Öffne im Browser: http://127.0.0.1:${PORT}/"
    exit 0
  fi
  sleep 0.2
done

echo "FEHLER: Server hat nicht geantwortet (siehe ${DIR}/server.log)" >&2
exit 1
