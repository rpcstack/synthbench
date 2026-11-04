#!/usr/bin/env bash
# Build the image for MODE, push it to the local registry, and deploy a topology
# to the kind cluster. Usage: scripts/k8s-deploy.sh [topology]   (default: ci)
#   MODE=baseline|masa  REG_PORT=5001  CLUSTER=rpcstack-synthbench  NAMESPACE=synthbench
#   SKIP_BUILD=1 reuses an image already in the registry.
set -euo pipefail
cd "$(dirname "$0")/.."
TOPOLOGY="${1:-ci}"
MODE="${MODE:-baseline}"
REG_PORT="${REG_PORT:-5001}"
CLUSTER="${CLUSTER:-rpcstack-synthbench}"
NAMESPACE="${NAMESPACE:-synthbench}"
IMG="localhost:${REG_PORT}/rpcstack-synthbench:${MODE}"
CTX="kind-${CLUSTER}"

case "$MODE" in baseline|masa) ;; *) echo "MODE must be baseline or masa" >&2; exit 1;; esac
[ -f "deploy/${TOPOLOGY}/k8s.yaml" ] || { echo "unknown topology ${TOPOLOGY}" >&2; exit 1; }

if [ "${SKIP_BUILD:-0}" != 1 ]; then
  docker build --build-arg "MODE=${MODE}" -t "$IMG" .
  docker push "$IMG"
fi

kubectl --context "$CTX" create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
# Checked-in manifests reference localhost:5001/rpcstack-synthbench:baseline; point them at this run.
scripts/lib/render-k8s.sh "deploy/${TOPOLOGY}/k8s.yaml" | kubectl --context "$CTX" -n "$NAMESPACE" apply -f -
for d in $(kubectl --context "$CTX" -n "$NAMESPACE" get deployment -o name); do
  kubectl --context "$CTX" -n "$NAMESPACE" rollout status "$d" --timeout=300s
done
echo "deployed ${TOPOLOGY} (${MODE}); run scripts/smoke-k8s.sh ${MODE}"
