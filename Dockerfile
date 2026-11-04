# syntax=docker/dockerfile:1
# One build stage, one runtime image per binary (frontend, child, loadgen).
#   docker build --target child --build-arg MODE=masa -t synthbench-child:masa .
# MODE is `baseline` (no-op hooks) or `masa` (composed Masa policy preset).
FROM rust:1-slim-bookworm AS builder
ARG MODE=masa
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY rust-toolchain.toml Cargo.toml Cargo.lock ./
COPY crates crates
# rust-toolchain.toml makes rustup install the pinned nightly on first use.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/src/target,id=synthbench-target-${MODE} \
    case "$MODE" in baseline|masa) ;; *) echo "MODE must be baseline or masa" >&2; exit 1;; esac \
    && cargo build --locked --release -p synthbench --bins --features "$MODE" \
    && mkdir -p /out && cp target/release/synthbench_frontend target/release/synthbench_child \
       target/release/synthbench_client_bench target/release/validate_config /out/

FROM debian:bookworm-slim AS runtime-base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
COPY docker/entrypoint.sh /usr/entrypoint.sh
ENV LOG_LEVEL=info
ENTRYPOINT ["/usr/entrypoint.sh"]

FROM runtime-base AS frontend
COPY --from=builder /out/synthbench_frontend /usr/local/bin/
ENV BINARY_NAME=synthbench_frontend
EXPOSE 8000

FROM runtime-base AS child
COPY --from=builder /out/synthbench_child /usr/local/bin/
ENV BINARY_NAME=synthbench_child
EXPOSE 8000

FROM runtime-base AS loadgen
COPY --from=builder /out/synthbench_client_bench /usr/local/bin/
ENV BINARY_NAME=synthbench_client_bench

FROM runtime-base AS validate
COPY --from=builder /out/validate_config /usr/local/bin/
ENV BINARY_NAME=validate_config
