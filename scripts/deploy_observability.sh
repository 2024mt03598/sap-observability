#!/bin/bash
# Runs ON the observability VM (r3server-tf) as root, via `az vm run-command`.
# Expects env var PAYLOAD = base64 tar.gz containing: prometheus/ alertmanager/ grafana/
# For each component: skip if unchanged, otherwise install, validate, restart ONLY that
# service, and roll back automatically if validation or the restart fails.
set -u
FAILED=0
STAGE=/tmp/cfg-stage
BACKUP_ROOT=/var/backups/config-deploy

rm -rf "$STAGE"; mkdir -p "$STAGE" "$BACKUP_ROOT"
echo "$PAYLOAD" | base64 -d | tar xz -C "$STAGE" || { echo "Could not unpack payload"; echo "DEPLOY_RESULT=FAIL"; exit 1; }

hash_dir() {
  if [ -d "$1" ]; then
    (cd "$1" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum | sha256sum)
  else
    echo "none"
  fi
}

# deploy <name> <staged-dir> <live-dir> <service> <owner> <validate-command>
deploy() {
  local name="$1" src="$2" dst="$3" svc="$4" owner="$5" check="$6"
  [ -d "$src" ] || { echo "[$name] not in payload, skipping"; return 0; }
  mkdir -p "$dst"

  if [ "$(hash_dir "$src")" = "$(hash_dir "$dst")" ]; then
    echo "[$name] unchanged - nothing to do"
    return 0
  fi

  echo "[$name] changed - deploying"
  local bak="$BACKUP_ROOT/$name-$(date +%Y%m%d%H%M%S)"
  mkdir -p "$bak"; cp -a "$dst/." "$bak/"

  rollback() {
    echo "[$name] ROLLING BACK"
    find "$dst" -mindepth 1 -delete
    cp -a "$bak/." "$dst/"
    chown -R "$owner" "$dst"
    systemctl restart "$svc" || true
    FAILED=1
  }

  find "$dst" -mindepth 1 -delete
  cp -a "$src/." "$dst/"
  chown -R "$owner" "$dst"

  if ! eval "$check"; then
    echo "[$name] validation FAILED"
    rollback
    return 0
  fi

  systemctl restart "$svc"
  sleep 8
  if ! systemctl is-active --quiet "$svc"; then
    echo "[$name] service $svc did not come back up"
    journalctl -u "$svc" -n 20 --no-pager || true
    rollback
    return 0
  fi
  echo "[$name] OK - $svc restarted"
}

deploy alertmanager "$STAGE/alertmanager" /etc/alertmanager alertmanager alertmanager:alertmanager \
  "/usr/local/bin/amtool check-config /etc/alertmanager/alertmanager.yml"

deploy prometheus "$STAGE/prometheus" /etc/prometheus prometheus prometheus:prometheus \
  "/usr/local/bin/promtool check config /etc/prometheus/prometheus.yml"

deploy grafana-datasources "$STAGE/grafana/provisioning/datasources" /etc/grafana/provisioning/datasources grafana-server root:grafana \
  "true"

echo "--- service status ---"
for s in prometheus alertmanager grafana-server node_exporter; do
  printf '%s: ' "$s"; systemctl is-active "$s" || true
done
curl -s -o /dev/null -w "prometheus ready: %{http_code}\n" localhost:9090/-/ready || true

if [ "$FAILED" -eq 0 ]; then echo "DEPLOY_RESULT=OK"; else echo "DEPLOY_RESULT=FAIL"; exit 1; fi
