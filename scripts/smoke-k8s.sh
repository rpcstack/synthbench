#!/usr/bin/env bash
# End-to-end smoke test of the Kubernetes (kind) deployment; same assertions
# as scripts/smoke-compose.sh.
#
#   scripts/kind-up.sh && scripts/smoke-k8s.sh [baseline|masa]
#
# Environment: TOPOLOGY (default ci), REG_PORT (5001), CLUSTER
# (rpcstack-synthbench), NAMESPACE (synthbench), SMOKE_MIN_OK_RATE (0.99),
# SKIP_DEPLOY=1 to reuse a running deployment, KEEP_UP=1 to keep the namespace.
set -euo pipefail

MODE="${1:-${MODE:-baseline}}"
case "$MODE" in baseline|masa) ;; *) echo "usage: $0 [baseline|masa]" >&2; exit 2 ;; esac
export MODE
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TOPOLOGY="${TOPOLOGY:-ci}"
export REG_PORT="${REG_PORT:-5001}"
CLUSTER="${CLUSTER:-rpcstack-synthbench}"
NAMESPACE="${NAMESPACE:-synthbench}"
MIN_OK="${SMOKE_MIN_OK_RATE:-0.99}"
OUT="${SMOKE_OUT:-$ROOT/out/smoke-k8s-$MODE}"
IMG="localhost:${REG_PORT}/rpcstack-synthbench:${MODE}"
mkdir -p "$OUT"
K=(kubectl --context "kind-${CLUSTER}" -n "$NAMESPACE")
# shellcheck disable=SC2207
TARGETS=($(python3 scripts/gen-deploy.py "config/$TOPOLOGY/topology.json" --out /dev/null --probe-args))

cleanup() {
  status=$?
  for p in $("${K[@]}" get pods -o name 2>/dev/null); do
    { echo "=== $p"; "${K[@]}" logs "$p" --all-containers 2>&1 || true; } >> "$OUT/pods.log"
  done
  if [ "${KEEP_UP:-0}" != 1 ]; then
    kubectl --context "kind-${CLUSTER}" delete namespace "$NAMESPACE" --wait=true --timeout=180s > /dev/null 2>&1 || true
  fi
  [ $status -eq 0 ] && echo "SMOKE PASS (k8s, $MODE)" || echo "SMOKE FAIL (k8s, $MODE), logs in $OUT"
  exit $status
}
trap cleanup EXIT
: > "$OUT/pods.log"

if [ "${SKIP_DEPLOY:-0}" != 1 ]; then
  echo "== build, push and deploy ($MODE)"
  CLUSTER="$CLUSTER" NAMESPACE="$NAMESPACE" scripts/k8s-deploy.sh "$TOPOLOGY"
fi

# Run the image once as a pod with the given args; echo its logs; return its exit code.
run_pod() {
  local name="$1"; shift
  "${K[@]}" delete pod "$name" --ignore-not-found --wait=true > /dev/null
  "${K[@]}" run "$name" --image "$IMG" --image-pull-policy=IfNotPresent --restart=Never \
    --env LOG_LEVEL=info -- "$@" > /dev/null
  local code=""
  until [ -n "$code" ]; do
    code=$("${K[@]}" get pod "$name" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2> /dev/null || true)
    [ -n "$code" ] || sleep 2
  done
  "${K[@]}" logs "$name"
  return "$code"
}
probe() { run_pod sb-probe probe --wait-secs 300 --frontend http://frontend:8000 "${TARGETS[@]}" "$@"; }

# Run the load-generator Job on smoke/<name>.json (or gen_config.json); traces to stdout.
load() {
  local cfg="smoke-$1.json"
  "${K[@]}" delete job sb-loadgen --ignore-not-found --wait=true > /dev/null
  scripts/lib/render-k8s.sh "deploy/$TOPOLOGY/k8s-loadgen.yaml" \
    | sed -e "s#/gen_config.json#/$cfg#" | "${K[@]}" apply -f - > /dev/null
  if ! "${K[@]}" wait --for=condition=complete --timeout=300s job/sb-loadgen > /dev/null; then
    "${K[@]}" logs job/sb-loadgen | tail -20; return 1
  fi
  "${K[@]}" logs job/sb-loadgen
}
check() { python3 "$ROOT/scripts/lib/check_traces.py" "$@"; }

echo "image mode: $(run_pod sb-mode mode)"
echo "== policy-liveness probe (expect $MODE)"
probe --expect "$MODE" | tee "$OUT/probe.txt"
if [ "$MODE" = baseline ]; then
  echo "== policy-liveness probe (expect masa) must FAIL in baseline"
  if probe --expect masa > "$OUT/probe-expect-masa.txt" 2>&1; then
    cat "$OUT/probe-expect-masa.txt"; echo "FAIL: the masa policy-live check passed in baseline mode"; exit 1
  fi
  grep -q "PROBE FAIL" "$OUT/probe-expect-masa.txt" || { cat "$OUT/probe-expect-masa.txt"; echo "FAIL: negative probe did not report PROBE FAIL" >&2; exit 1; }
  tail -3 "$OUT/probe-expect-masa.txt"
  echo "policy-live check failed in baseline, as required"
fi

echo "== moderate load"
load moderate > "$OUT/moderate.txt"
if [ "$MODE" = masa ]; then
  check "$OUT/moderate.txt" --label moderate --min-requests 500 --min-ok-rate "$MIN_OK" --max-transport 0
else
  check "$OUT/moderate.txt" --label moderate --min-requests 500 --min-ok-rate "$MIN_OK" --max-transport 0 --max-rejected 0
fi

if [ "$MODE" = masa ]; then
  echo "== overload burst"
  load overload > "$OUT/overload.txt"
  check "$OUT/overload.txt" --label overload --min-rejected 1
  echo "== recovery"
  load recovery > "$OUT/recovery.txt"
  check "$OUT/recovery.txt" --label recovery --tail-secs 5 --min-requests 150 --min-ok-rate "$MIN_OK" --max-transport 0
fi

echo "== probe again"
probe --expect "$MODE" | tee "$OUT/probe-after-overload.txt"

echo "== crash check"
restarts=$("${K[@]}" get pods -o jsonpath='{range .items[*]}{.metadata.name} {.status.containerStatuses[0].restartCount}{"\n"}{end}' | awk '$2 > 0')
if [ -n "$restarts" ]; then echo "FAIL: restarted pods: $restarts"; exit 1; fi
for p in $("${K[@]}" get pods -o name); do
  if "${K[@]}" logs "$p" 2>&1 | grep -q panicked; then echo "FAIL: panic in $p"; exit 1; fi
done
echo "no panics, no restarts"
