#!/usr/bin/env bash
# Kubernetes (kind) smoke test, same assertions as compose-smoke.sh.
# Usage: scripts/k8s-smoke.sh [baseline|masa]   (deploys first via k8s-deploy.sh)
#   TOPOLOGY=ci  MIN_SUCCESS=0.95  REG_PORT=5001  CLUSTER=rpcstack-synthbench
#   NAMESPACE=synthbench  SKIP_DEPLOY=1  KEEP=1
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-masa}"
TOPOLOGY="${TOPOLOGY:-ci}"
MIN_SUCCESS="${MIN_SUCCESS:-0.95}"
REG_PORT="${REG_PORT:-5001}"
CLUSTER="${CLUSTER:-rpcstack-synthbench}"
NAMESPACE="${NAMESPACE:-synthbench}"
REG="localhost:${REG_PORT}"
case "$MODE" in baseline|masa) ;; *) echo "usage: $0 [baseline|masa]" >&2; exit 2;; esac
K=(kubectl --context "kind-${CLUSTER}" -n "$NAMESPACE")
# shellcheck disable=SC2207
TARGETS=($(python3 scripts/gen-deploy.py "config/${TOPOLOGY}/topology.json" --out /dev/null --probe-args))

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    kubectl --context "kind-${CLUSTER}" delete namespace "$NAMESPACE" --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[ "${SKIP_DEPLOY:-0}" = 1 ] || MODE="$MODE" REG_PORT="$REG_PORT" CLUSTER="$CLUSTER" NAMESPACE="$NAMESPACE" \
  scripts/k8s-deploy.sh "$TOPOLOGY"

# Run the probe image as a one-off pod; echo its logs; return its exit code.
probe() {
  "${K[@]}" delete pod sb-probe --ignore-not-found --wait=true >/dev/null
  "${K[@]}" run sb-probe --image "${REG}/synthbench-probe:${MODE}" --restart=Never \
    --image-pull-policy=IfNotPresent -- --frontend http://frontend:8000 --wait-secs 90 "${TARGETS[@]}" "$@" >/dev/null
  until [ -n "$("${K[@]}" get pod sb-probe -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null)" ]; do sleep 2; done
  "${K[@]}" logs sb-probe
  return "$("${K[@]}" get pod sb-probe -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}')"
}

# Run the loadgen Job with a given gen_*.json; print its output.
loadgen() {
  "${K[@]}" delete job sb-loadgen --ignore-not-found --wait=true >/dev/null
  sed -e "s#localhost:5001/#${REG}/#g" -e "s#\(synthbench-[a-z]*\):masa#\1:${MODE}#g" \
      -e "s#/usr/loadgen/gen_config.json#/usr/loadgen/$1#" "deploy/${TOPOLOGY}/k8s-loadgen.yaml" \
    | "${K[@]}" apply -f - >/dev/null
  "${K[@]}" wait --for=condition=complete --timeout=300s job/sb-loadgen >/dev/null \
    || { "${K[@]}" logs job/sb-loadgen | tail -20; echo "FAIL: loadgen job did not complete"; return 1; }
  "${K[@]}" logs job/sb-loadgen
}

echo "== policy-live (expect masa)"
if probe --expect masa; then probe_masa=pass; else probe_masa=fail; fi
if [ "$MODE" = masa ]; then
  [ "$probe_masa" = pass ] || { echo "FAIL: policy-live check failed in masa mode"; exit 1; }
else
  [ "$probe_masa" = fail ] || { echo "FAIL: policy-live check passed in baseline mode; it must not"; exit 1; }
  echo "policy-live correctly FAILED in baseline"
  probe --expect baseline >/dev/null && echo "baseline probe: no Masa rejections, as expected"
fi

echo "== steady load"
out=$(loadgen gen_smoke.json)
echo "$out" | python3 scripts/analyze-traces.py --label steady --min-success "$MIN_SUCCESS" --min-requests 100

if [ "$MODE" = masa ]; then
  echo "== overload"
  out=$(loadgen gen_overload.json)
  echo "$out" | python3 scripts/analyze-traces.py --label overload --min-early-returns 1 --min-requests 100
  echo "== recovery"
  out=$(loadgen gen_smoke.json)
  echo "$out" | python3 scripts/analyze-traces.py --label recovery --min-success "$MIN_SUCCESS" --min-requests 100
fi
echo "SMOKE PASS (k8s, ${MODE}, ${TOPOLOGY})"
