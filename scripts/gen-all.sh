#!/usr/bin/env bash
# Regenerate deploy/<topology>/ for every topology under config/.
set -euo pipefail
cd "$(dirname "$0")/.."
for dir in config/*/; do
  name=$(basename "$dir")
  python3 scripts/gen-deploy.py "config/$name/topology.json" \
    --loadgen-dir "config/$name" --out "deploy/$name"
done
