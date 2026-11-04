#!/usr/bin/env bash
# Create a kind cluster wired to a local image registry (the standard kind
# "local registry" recipe). No images are published anywhere; everything is
# built locally and pushed to this registry.
#   CLUSTER=rpcstack-synthbench REG_NAME=rpcstack-synthbench-registry REG_PORT=5001
set -euo pipefail
CLUSTER="${CLUSTER:-rpcstack-synthbench}"
REG_NAME="${REG_NAME:-rpcstack-synthbench-registry}"
REG_PORT="${REG_PORT:-5001}"

if [ "$(docker inspect -f '{{.State.Running}}' "$REG_NAME" 2>/dev/null || true)" != "true" ]; then
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${REG_PORT}\$"; then
    echo "port ${REG_PORT} is already in use; set REG_PORT to a free port" >&2; exit 1
  fi
  docker run -d --restart=always -p "127.0.0.1:${REG_PORT}:5000" --network bridge --name "$REG_NAME" registry:2
fi

if ! kind get clusters | grep -qx "$CLUSTER"; then
  cat <<YAML | kind create cluster --name "$CLUSTER" --wait 120s --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
  - |-
    [plugins."io.containerd.grpc.v1.cri".registry]
      config_path = "/etc/containerd/certs.d"
YAML
fi

# Tell every node that localhost:${REG_PORT} is the registry container.
for node in $(kind get nodes --name "$CLUSTER"); do
  docker exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${REG_PORT}"
  cat <<TOML | docker exec -i "$node" tee "/etc/containerd/certs.d/localhost:${REG_PORT}/hosts.toml" >/dev/null
[host."http://${REG_NAME}:5000"]
TOML
done
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REG_NAME")" = "null" ]; then
  docker network connect kind "$REG_NAME"
fi
echo "kind cluster ${CLUSTER} ready; registry localhost:${REG_PORT}"
