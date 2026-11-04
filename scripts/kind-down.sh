#!/usr/bin/env bash
# Remove only what kind-up.sh created.
set -euo pipefail
CLUSTER="${CLUSTER:-rpcstack-synthbench}"
REG_NAME="${REG_NAME:-rpcstack-synthbench-registry}"
kind delete cluster --name "$CLUSTER" || true
docker rm -f "$REG_NAME" 2>/dev/null || true
