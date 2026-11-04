# syntax=docker/dockerfile:1.7
#
# One builder compiles every synthbench binary once; one runtime image carries
# them all and docker/entrypoint.sh picks the binary from the first argument.
#
#   docker build --build-arg MODE=masa -t rpcstack-synthbench:masa .
#
# MODE selects the policy preset (Cargo feature of the synthbench crate):
#   baseline  generated stubs use NoopHooks (no policy)
#   masa      sched_slo,abort_slo,signal_slack,est_mean_var,ac_pred
# FEATURES, if set, overrides MODE with an explicit Cargo feature list.

ARG RUST_IMAGE=rust:1-slim-bookworm
ARG RUNTIME_IMAGE=debian:bookworm-slim

FROM ${RUST_IMAGE} AS builder
ARG MODE=baseline
ARG FEATURES=
ARG BUILD_JOBS=
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git pkg-config \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# rust-toolchain.toml pins the nightly rpcstack needs; install it in its own layer.
COPY rust-toolchain.toml ./
RUN rustup show active-toolchain || rustup toolchain install
COPY Cargo.toml Cargo.lock ./
COPY crates crates
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/src/target,id=synthbench-target-${MODE} \
    set -eu; \
    features="${FEATURES:-}"; \
    if [ -z "$features" ]; then \
      case "$MODE" in \
        baseline) features=baseline ;; \
        masa) features=masa ;; \
        *) echo "unknown MODE=$MODE (expected baseline or masa)" >&2; exit 1 ;; \
      esac; \
    fi; \
    echo "building synthbench with features: $features"; \
    cargo build --locked --release -p synthbench --bins --features "$features" ${BUILD_JOBS:+-j $BUILD_JOBS}; \
    mkdir -p /out; \
    for f in synthbench_frontend synthbench_child synthbench_client_bench synthbench_policy_probe validate_config; do \
      cp "target/release/$f" /out/; \
    done; \
    echo "$MODE ${features}" > /out/SYNTHBENCH_MODE; \
    ls -l /out

FROM ${RUNTIME_IMAGE} AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/ /usr/local/bin/
RUN mv /usr/local/bin/SYNTHBENCH_MODE /etc/synthbench-mode
COPY docker/entrypoint.sh /usr/local/bin/synthbench-entrypoint
# The default topology and load configs are baked in; compose and k8s mount
# the selected topology over /etc/synthbench.
COPY config/ci/ /etc/synthbench/
ENV LOG_LEVEL=info \
    SYNTHBENCH_CONFIG=/etc/synthbench/topology.json \
    SYNTHBENCH_GEN_CONFIG=/etc/synthbench/gen_config.json
ENTRYPOINT ["/usr/local/bin/synthbench-entrypoint"]
CMD ["frontend"]
