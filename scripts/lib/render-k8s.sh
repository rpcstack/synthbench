#!/bin/sh
# Rewrite the image reference in a generated manifest to this run's registry and mode.
#   MODE=masa REG_PORT=5001 scripts/lib/render-k8s.sh deploy/ci/k8s.yaml
sed -e "s#localhost:5001/rpcstack-synthbench:baseline#localhost:${REG_PORT:-5001}/rpcstack-synthbench:${MODE:-baseline}#g" "$@"
