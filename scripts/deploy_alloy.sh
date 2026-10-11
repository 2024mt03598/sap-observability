#!/bin/bash
# Runs ON the SAP/Oracle VM (r3server) as root, via `az vm run-command`.
# Expects env var PAYLOAD = base64 tar.gz containing: alloy/r3server.alloy
# Replaces /etc/alloy/config.alloy only if it changed, restarts alloy, and rolls back
# to the previous file if alloy does not stay running.
set -u
STAGE=/tmp/alloy-stage
LIVE=/etc/alloy/config.alloy
BACKUP_ROOT=/var/backups/config-deploy

rm -rf "$STAGE"; mkdir -p "$STAGE" "$BACKUP_ROOT"
echo "$PAYLOAD" | base64 -d | tar xz -C "$STAGE" || { echo "Could not unpack payload"; echo "DEPLOY_RESULT=FAIL"; exit 1; }

NEW="$STAGE/alloy/r3server.alloy"
[ -f "$NEW" ] || { echo "alloy/r3server.alloy missing from payload"; echo "DEPLOY_RESULT=FAIL"; exit 1; }

if cmp -s "$NEW" "$LIVE"; then
  echo "[alloy] unchanged - nothing to do"
  echo "DEPLOY_RESULT=OK"
  exit 0
fi

echo "[alloy] changed - deploying"
BAK="$BACKUP_ROOT/config.alloy-$(date +%Y%m%d%H%M%S)"
cp -a "$LIVE" "$BAK"

# cat > keeps the existing file's owner and permissions
cat "$NEW" > "$LIVE"
systemctl reset-failed alloy 2>/dev/null || true
systemctl restart alloy
sleep 15

if systemctl is-active --quiet alloy; then
  echo "[alloy] OK - alloy restarted and running"
  systemctl status alloy --no-pager | head -5
  echo "DEPLOY_RESULT=OK"
else
  echo "[alloy] alloy failed to start - last log lines:"
  journalctl -u alloy -n 30 --no-pager || true
  echo "[alloy] ROLLING BACK to $BAK"
  cat "$BAK" > "$LIVE"
  systemctl reset-failed alloy 2>/dev/null || true
  systemctl restart alloy || true
  echo "DEPLOY_RESULT=FAIL"
  exit 1
fi
