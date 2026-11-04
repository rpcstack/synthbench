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
| `baseline` (default) | rpcstack's no-op hooks: plain gRPC, FIFO, no policy |
| `masa` | the composed Masa policy preset from rpcstack's `examples/masa` (`sched_slo`, `abort_slo`, `signal_slack`, `est_mean_var`, `ac_pred`) |

Scheduling operates between future polls; there is no forced preemption.

## Quick start

Everything is built locally; no images are published. Compose needs Docker with
Compose v2.20+; Kubernetes needs `kind` and `kubectl`.

### Docker Compose

```sh
docker compose up --build                  # baseline (no policy), config/ci topology
MODE=masa docker compose up --build        # Masa composed policy
docker compose -f deploy/service-time-shift/docker-compose.yaml up --build
```

One image (`rpcstack-synthbench:<mode>`) carries every binary; `docker/entrypoint.sh`
selects the role from the first argument (`frontend`, `child`, `client-bench`,
`probe`, `validate`, `mode`). The load generator runs once after the frontend
starts and prints its CSV traces to stdout (`---BEGIN TRACES---`); re-run it with
`docker compose run --rm client-bench`.

### Kubernetes (kind + local registry)

```sh
scripts/kind-up.sh                        # kind cluster + local registry (REG_PORT=5001)
MODE=masa scripts/k8s-deploy.sh ci        # build, push, apply, wait for rollout
scripts/kind-down.sh
```

`deploy/<topology>/k8s.yaml` holds one Deployment and Service per replica plus a
ConfigMap with the topology; `k8s-loadgen.yaml` is a one-shot load-generator Job.

### Smoke tests

```sh
scripts/smoke-compose.sh masa          # or baseline
scripts/kind-up.sh && scripts/smoke-k8s.sh masa
```

Both use real RPCs through the generated stubs and assert:

1. **policy-live**: `synthbench_policy_probe` first sends control requests with a
   generous SLO (must succeed in both modes), then requests whose Masa deadline
   already expired to the frontend and to each child service directly. Masa's
   deadline guard answers `/EarlyReturn`; no-op hooks cannot. `--expect masa`
   requires an `/EarlyReturn` from every target, `--expect baseline` requires
   none. In baseline the scripts also run `--expect masa` and require it to **fail**.
2. **moderate load** (`config/<topology>/smoke/moderate.json`): zero transport
   errors, ok rate at least `SMOKE_MIN_OK_RATE` (0.99); baseline shows no rejections.
3. **masa only**: an overload burst (`overload.json`) must be shed with at least
   one `/EarlyReturn`, then `recovery.json` must reach the ok rate with zero
   transport errors in its last 5 s.
4. the probe runs again, and no pod/container restarted or panicked.

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
default run and `smoke/{moderate,overload,recovery}.json` are used by the smoke
tests (`Rps`, `Slos`, `Apis`, `DurationSecs`, ...). The topology directory is
mounted at `/etc/synthbench`; pick another file with
`docker compose run --rm -e SYNTHBENCH_GEN_CONFIG=/etc/synthbench/smoke/overload.json client-bench client-bench`.

### Deployments are generated from the topology

Services find each other by hostname, `sb-local-<id>-service-<n>` (id lower-cased,
`_` to `-`), so the layout follows from the topology.
`scripts/gen-deploy.py` renders `deploy/<name>/docker-compose.yaml` and
`k8s.yaml` (plus `k8s-loadgen.yaml`); `scripts/gen-all.sh` regenerates every
topology, and a unit test fails if the checked-in files are stale. To add a topology,
create `config/<name>/{topology.json,gen_config.json,smoke/*.json}` and run
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
