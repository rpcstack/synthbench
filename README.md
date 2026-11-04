# synthbench

A configurable synthetic microservice benchmark built on
[rpcstack](https://github.com/rpcstack/rpcstack). You describe a tree (or DAG)
of services in a JSON topology: each service has replicas and methods, each
method has a latency distribution, a busy-spin fraction and a probabilistic
call sequence to other `service::method` targets. synthbench runs one
`frontend`, one process per child-service replica, and a load generator that
drives the frontend, so you can study how an RPC-level policy behaves on a
call graph whose shape and service times you control.

Two build-time modes:

| mode | what runs |
|------|-----------|
| `baseline` | rpcstack's no-op hooks: plain gRPC, FIFO, no policy |
| `masa` | the composed Masa policy preset from rpcstack's `examples/masa` (`sched_slo`, `abort_slo`, `signal_slack`, `est_mean_var`, `ac_pred`) |

Scheduling operates between future polls; there is no forced preemption.

## Quick start

Everything is built locally; no images are published.

### Docker Compose

```sh
docker compose up --build                      # masa mode, config/ci topology
SYNTHBENCH_MODE=baseline docker compose up --build
docker compose -f deploy/service-time-shift/docker-compose.yaml up --build
```

The load generator runs once after the frontend starts and prints its CSV
traces to stdout (`---BEGIN TRACES---`).

### Kubernetes (kind + local registry)

```sh
scripts/kind-up.sh                       # kind cluster + local registry (REG_PORT=5001)
MODE=masa scripts/k8s-deploy.sh ci       # build, push, apply, wait for rollout
scripts/k8s-smoke.sh masa                # deploys, then asserts (see Smoke tests)
scripts/kind-down.sh
```

### Smoke tests

```sh
scripts/compose-smoke.sh masa      # or baseline
scripts/k8s-smoke.sh masa          # needs scripts/kind-up.sh first
```

Both assert, using real RPCs through the generated stubs:

1. **policy-live**: `synthbench_policy_probe --expect masa` sends requests whose
   Masa deadline already expired to the frontend and to each child service
   directly. Masa's deadline guard answers `/EarlyReturn`; no-op hooks cannot.
   The check passes in `masa` mode and must **fail** in `baseline` mode; the
   scripts require exactly that.
2. **steady load**: zero unexpected (transport/application) errors and a success
   fraction of at least `MIN_SUCCESS` (default 0.95).
3. **masa only**: a short overload run produces policy rejections and no
   transport errors, then a steady run recovers to `MIN_SUCCESS`.

## Topology format

A topology file (`config/<name>/topology.json`) is a `SynthbenchConfig`:

```json
{
  "call_graph": {
    "entry_points": { "a": [ { "front::run": 1.0 } ] },
    "services": [
      { "id": "front", "replicas": 2,
        "methods": [ { "name": "run",
          "latency_distribution": { "Exponential": { "mean": 10000 } },
          "busy_spin_ratio": 0.1,
          "call_sequence": [ { "leaf::work": 1.0 }, { "leaf::work": 0.3 } ] } ] },
      { "id": "leaf", "replicas": 1,
        "methods": [ { "name": "work",
          "latency_distribution": { "Normal": { "mean": 5000, "std": 500 } },
          "call_sequence": [] } ] }
    ],
    "child_cpus_per_replica": 1.0
  }
}
```

- `entry_points`: frontend API (`a` or `b`) to a call sequence started by the frontend.
- `call_sequence`: a list of steps run **sequentially**; each step is a map of
  `service::method` to call probability, and the calls inside one step run
  **in parallel**.
- `latency_distribution` (microseconds): `Exponential` (`mean` or `lambda`),
  `Normal` (`mean`, `std`), `LogNormal`, `Discrete` (`weights`, `values`),
  `Periodic` (`slow_latency`, `fast_latency`, `slow_duration_ms`),
  `ShiftedNormal` (`after_secs`, `before_*`, `after_*`). See
  `crates/synthbench/src/distribution.rs` for the authoritative list.
- `busy_spin_ratio`: fraction of the sampled time spent spinning the CPU rather than sleeping.
- `child_cpus_per_replica`: CPU limit and runtime worker threads per replica.

Validate a topology (parse plus call-graph checks: missing targets, bad names):

```sh
cargo run -q --release -p synthbench --bin validate_config -- --config config/ci/topology.json
```

Load generator files live next to the topology: `gen_config.json` is the
default run, `gen_smoke.json` and `gen_overload.json` are used by the smoke
tests (`Rps`, `Slos`, `Apis`, `DurationSecs`, ...). Select one with
`GEN_CONFIG=/usr/loadgen/gen_overload.json docker compose run --rm loadgen`.

### Deployments are generated from the topology

Services find each other by hostname, `sb-local-<id>-service-<n>` (id lower-cased,
`_` to `-`), so the layout follows from the topology.
`scripts/gen-deploy.py` renders `deploy/<name>/docker-compose.yaml` and
`k8s.yaml` (plus `k8s-loadgen.yaml`); `scripts/gen-all.sh` regenerates every
topology, and a unit test fails if the checked-in files are stale. To add a topology,
create `config/<name>/{topology,gen_config,gen_smoke,gen_overload}.json` and run
`scripts/gen-all.sh`.

## Development

```sh
./scripts/check.sh    # rustfmt, single pinned rpcstack, single tokio/hyper, warning-free check (both modes)
./scripts/test.sh     # cargo tests in both modes + generator/analyzer tests
./scripts/format.sh
```

`crates/synthbench/build.rs` sets `.default_hooks_path("masa::DefaultHooks")` on
every generated stub; `crates/synthbench/tests/hooks.rs` fails if a stub falls back
to no-op hooks.

## Provenance

Ported from `vic-lsh/masa` `apps/synthbench` at commit `c8486eb`, built on
rpcstack `de0cedc080882469b316c8125b7ad17a7c93e7fb` (pinned in `Cargo.toml`).
The `ci` and `service-time-shift` topologies come from masa's
`exp/synthbench/in/`. The experiment runner, evaluation configs and policy
sweeps are not included. `app-util-macros` is not ported: nothing in synthbench
uses it.
