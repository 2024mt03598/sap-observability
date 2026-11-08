#!/bin/bash
# Runs ON the SAP host (r3server) as root, via `az vm run-command`.
# Expects env var PAYLOAD = base64 tar.gz containing: sapcontrol/sapcontrol_exporter.py and sapcontrol-exporter.service
# Installs the exporter, restarts the service only if something changed, checks that it answers,
# and restores the previous version if it does not.
set -u
STAGE=/tmp/sapctl-stage
BIN=/usr/local/bin/sapcontrol_exporter.py
UNIT=/etc/systemd/system/sapcontrol-exporter.service
BACKUP=/var/backups/config-deploy/sapcontrol-$(date +%Y%m%d%H%M%S)

rm -rf "$STAGE"; mkdir -p "$STAGE" "$BACKUP"
echo "$PAYLOAD" | base64 -d | tar xz -C "$STAGE" || { echo "Could not unpack payload"; echo "DEPLOY_RESULT=FAIL"; exit 1; }
NEWBIN="$STAGE/sapcontrol/sapcontrol_exporter.py"
NEWUNIT="$STAGE/sapcontrol/sapcontrol-exporter.service"
[ -f "$NEWBIN" ] && [ -f "$NEWUNIT" ] || { echo "payload incomplete"; echo "DEPLOY_RESULT=FAIL"; exit 1; }

if cmp -s "$NEWBIN" "$BIN" && cmp -s "$NEWUNIT" "$UNIT" && systemctl is-active --quiet sapcontrol-exporter; then
  echo "[sapcontrol-exporter] unchanged and running - nothing to do"
  echo "DEPLOY_RESULT=OK"; exit 0
fi

python3 -m py_compile "$NEWBIN" || { echo "python syntax check failed"; echo "DEPLOY_RESULT=FAIL"; exit 1; }
[ -f "$BIN" ] && cp -a "$BIN" "$BACKUP/"
[ -f "$UNIT" ] && cp -a "$UNIT" "$BACKUP/"

install -m 0755 "$NEWBIN" "$BIN"
install -m 0644 "$NEWUNIT" "$UNIT"
systemctl daemon-reload
systemctl enable sapcontrol-exporter >/dev/null 2>&1 || true
systemctl restart sapcontrol-exporter
sleep 5

PORT=$(grep -E '^SAPCONTROL_LISTEN=' /etc/sapcontrol-exporter.env 2>/dev/null | cut -d: -f2)
PORT=${PORT:-9681}
if systemctl is-active --quiet sapcontrol-exporter && curl -fsS "http://127.0.0.1:$PORT/metrics" | grep -q '^sapcontrol_up'; then
  echo "[sapcontrol-exporter] OK"
  curl -s "http://127.0.0.1:$PORT/metrics" | grep -E '^sapcontrol_(up|exporter_info)'
  echo "DEPLOY_RESULT=OK"
else
  echo "[sapcontrol-exporter] failed to answer - last log lines:"
  journalctl -u sapcontrol-exporter -n 20 --no-pager || true
  echo "[sapcontrol-exporter] ROLLING BACK"
  if [ -f "$BACKUP/sapcontrol_exporter.py" ]; then
    install -m 0755 "$BACKUP/sapcontrol_exporter.py" "$BIN"
    [ -f "$BACKUP/sapcontrol-exporter.service" ] && install -m 0644 "$BACKUP/sapcontrol-exporter.service" "$UNIT"
    systemctl daemon-reload; systemctl restart sapcontrol-exporter || true
  else
    systemctl disable --now sapcontrol-exporter >/dev/null 2>&1 || true
    rm -f "$BIN" "$UNIT"; systemctl daemon-reload
  fi
  echo "DEPLOY_RESULT=FAIL"; exit 1
fi
