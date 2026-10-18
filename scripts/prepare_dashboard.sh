#!/bin/bash
# Turn a dashboard JSON downloaded from grafana.com (or exported with "Export for sharing
# externally") into a file that provisioning can use, and put it in config/grafana/dashboards/.
#
#   scripts/prepare_dashboard.sh ~/Downloads/node-exporter-full.json node-exporter-full
#
# What it does: replaces the ${DS_...} datasource placeholders with the fixed datasource uid
# "prometheus", and removes the "__inputs" import section that only the Import screen uses.
set -eu
SRC="${1:?usage: prepare_dashboard.sh <downloaded.json> <short-name>}"
NAME="${2:?usage: prepare_dashboard.sh <downloaded.json> <short-name>}"
OUT="$(dirname "$0")/../config/grafana/dashboards/$NAME.json"

python3 - "$SRC" "$OUT" <<'PY'
import json, re, sys
src, out = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
text = re.sub(r"\$\{DS_[A-Za-z0-9_\-]+\}", "prometheus", text)
d = json.loads(text)
d.pop("__inputs", None)
d.pop("__requires", None)
d["id"] = None                      # let Grafana assign it
d.setdefault("uid", re.sub(r"[^a-z0-9\-]", "-", out.split("/")[-1][:-5].lower())[:40])
json.dump(d, open(out, "w", encoding="utf-8"), indent=2)
print("wrote", out, "| uid:", d["uid"], "| title:", d.get("title"))
PY
