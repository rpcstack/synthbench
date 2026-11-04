#!/usr/bin/env bash
# Docker Compose smoke test. Usage: scripts/compose-smoke.sh [baseline|masa]
#   TOPOLOGY=ci  MIN_SUCCESS=0.95  KEEP=1 (leave the stack running)
# Builds locally, starts the topology, then asserts:
#   1. policy-live: `synthbench_policy_probe --expect masa` (expired deadlines
#      must be rejected with /EarlyReturn by the frontend and every service).
#      It must PASS in masa mode and FAIL in baseline mode.
#   2. a short steady load: zero unexpected (transport) errors, success >= MIN_SUCCESS.
#   3. masa only: a short overload run must trigger policy rejections without
#      transport errors, then a steady load must recover to >= MIN_SUCCESS.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-masa}"
TOPOLOGY="${TOPOLOGY:-ci}"
MIN_SUCCESS="${MIN_SUCCESS:-0.95}"
case "$MODE" in baseline|masa) ;; *) echo "usage: $0 [baseline|masa]" >&2; exit 2;; esac

export SYNTHBENCH_MODE="$MODE"
PROJECT="rpcstack-synthbench-${MODE}"
COMPOSE=(docker compose -p "$PROJECT" -f "deploy/${TOPOLOGY}/docker-compose.yaml")
# shellcheck disable=SC2207
TARGETS=($(python3 scripts/gen-deploy.py "config/${TOPOLOGY}/topology.json" --out /dev/null --probe-args))

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then "${COMPOSE[@]}" --profile tools down -v --remove-orphans >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

probe() { "${COMPOSE[@]}" run --rm --no-deps probe --frontend http://frontend:8000 --wait-secs 90 "${TARGETS[@]}" "$@"; }
loadgen() { "${COMPOSE[@]}" run --rm --no-deps -e "GEN_CONFIG=/usr/loadgen/$1" loadgen; }

echo "== build and start (${MODE}, ${TOPOLOGY})"
"${COMPOSE[@]}" up -d --build frontend

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
echo "SMOKE PASS (compose, ${MODE}, ${TOPOLOGY})"
