#!/bin/sh
# Select a synthbench binary by role.
#
#   frontend                  synthbench_frontend --config $SYNTHBENCH_CONFIG
#   child                     synthbench_child --config $SYNTHBENCH_CONFIG
#                             (the service it plays is $SERVICE_ID)
#   client-bench [args...]    load generator driven by $SYNTHBENCH_GEN_CONFIG
#   probe [args...]           policy-liveness probe (see synthbench_policy_probe)
#   validate [file]           validate a topology (default $SYNTHBENCH_CONFIG)
#   mode                      print the policy mode baked into this image
set -eu

export RUST_LOG="${RUST_LOG:-${LOG_LEVEL:-info}}"
role="${1:-}"
[ $# -gt 0 ] && shift

case "$role" in
  frontend|child)
    echo "synthbench: starting $role ($(cat /etc/synthbench-mode)) with $SYNTHBENCH_CONFIG" >&2
    exec "/usr/local/bin/synthbench_$role" --config "$SYNTHBENCH_CONFIG" "$@"
    ;;
  client-bench)
    out="${SYNTHBENCH_OUTPUT_PATH:-/tmp/synthbench-load-gen}"
    mkdir -p "$out"
    status=0
    /usr/local/bin/synthbench_client_bench --gen-config "$SYNTHBENCH_GEN_CONFIG" --output-path "$out" "$@" || status=$?
    # Emit the per-request traces so callers can collect them from the logs.
    echo "---BEGIN TRACES---"
    for f in "$out"/*.csv; do
      [ -f "$f" ] || continue
      echo "FILE: $(basename "$f")"
      cat "$f"
      echo "---END FILE---"
    done
    echo "---END TRACES---"
    exit $status
    ;;
  probe)
    exec /usr/local/bin/synthbench_policy_probe "$@"
    ;;
  validate)
    exec /usr/local/bin/validate_config --config "${1:-$SYNTHBENCH_CONFIG}"
    ;;
  mode)
    cat /etc/synthbench-mode
    ;;
  *)
    echo "usage: synthbench-entrypoint frontend|child|client-bench|probe|validate|mode [args...]" >&2
    exit 2
    ;;
esac
