#!/bin/sh
# Starts the binary named by BINARY_NAME. Services read /usr/config.json (the
# topology); the load generator reads /usr/gen_config.json and writes CSV
# traces to /tmp/synthbench-load, which are echoed to stdout when it finishes.
set -e
export RUST_LOG="${LOG_LEVEL:-info}"
bin="/usr/local/bin/${BINARY_NAME:?BINARY_NAME is not set}"
case "$BINARY_NAME" in
  *_client_bench)
    out=/tmp/synthbench-load; mkdir -p "$out"
    set +e
    "$bin" --gen-config /usr/gen_config.json --output-path "$out" "$@"
    rc=$?
    set -e
    echo "---BEGIN TRACES---"
    for f in "$out"/*.csv; do [ -f "$f" ] && { echo "FILE: $(basename "$f")"; cat "$f"; echo "---END FILE---"; }; done
    echo "---END TRACES---"
    exit $rc ;;
  *_policy_probe) exec "$bin" "$@" ;;
  validate_config) exec "$bin" --config /usr/config.json "$@" ;;
  *) exec "$bin" --config /usr/config.json "$@" ;;
esac
