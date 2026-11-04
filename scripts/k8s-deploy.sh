#!/usr/bin/env bash
# Build images for MODE, push them to the local registry, and deploy a topology
# to the kind cluster. Usage: scripts/k8s-deploy.sh [topology] (default: ci)
#   MODE=masa|baseline  REG_PORT=5001  CLUSTER=rpcstack-synthbench  NAMESPACE=synthbench
set -euo pipefail
cd "$(dirname "$0")/.."
TOPOLOGY="${1:-ci}"
MODE="${MODE:-masa}"
REG_PORT="${REG_PORT:-5001}"
CLUSTER="${CLUSTER:-rpcstack-synthbench}"
NAMESPACE="${NAMESPACE:-synthbench}"
REG="localhost:${REG_PORT}"
KCTL=(kubectl --context "kind-${CLUSTER}" -n "$NAMESPACE")

case "$MODE" in baseline|masa) ;; *) echo "MODE must be baseline or masa" >&2; exit 1;; esac
[ -f "deploy/${TOPOLOGY}/k8s.yaml" ] || { echo "unknown topology ${TOPOLOGY}" >&2; exit 1; }

for target in frontend child loadgen probe; do
  img="${REG}/synthbench-${target}:${MODE}"
  docker build --build-arg "MODE=${MODE}" --target "$target" -t "$img" .
  docker push "$img"
done

kubectl --context "kind-${CLUSTER}" create namespace "$NAMESPACE" --dry-run=client -o yaml \
  | kubectl --context "kind-${CLUSTER}" apply -f -
# Checked-in manifests use localhost:5001 and the masa tag; rewrite for this run.
render() { sed -e "s#localhost:5001/#${REG}/#g" -e "s#\(synthbench-[a-z]*\):masa#\1:${MODE}#g" "$@"; }
render "deploy/${TOPOLOGY}/k8s.yaml" | "${KCTL[@]}" apply -f -
"${KCTL[@]}" rollout status deployment --timeout=180s --all
echo "deployed ${TOPOLOGY} (${MODE}); run the load generator with scripts/k8s-smoke.sh"
