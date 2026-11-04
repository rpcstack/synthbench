#!/usr/bin/env bash
# Unit and regression tests in both policy modes.
set -euo pipefail
cd "$(dirname "$0")/.."
for features in baseline masa; do
  echo "== cargo test ($features)"
  cargo test --locked --workspace --features "synthbench/$features"
done

echo "== deployment generator"
python3 -I -m unittest discover -s tests
