#!/usr/bin/env python3
"""Generate Docker Compose and Kubernetes deployments from a synthbench topology.

    scripts/gen-deploy.py config/ci/topology.json --out deploy/ci

A topology is a synthbench config whose `call_graph.services` list names every
child service and its replica count. Synthbench processes find each other by
hostname, so the layout is fixed by the application, not chosen here:

    {PROJECT}-local-{service-id}-service-{replica}:8000

where service-id is lower-cased with `_` replaced by `-`, replica counts from 1,
and PROJECT is the DOCKER_COMPOSE_PROJECT_NAME env var given to every process.
Compose and Kubernetes both get one workload per replica named exactly that.
Only the standard library is used.
"""
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REGISTRY = "localhost:5001"
PROJECT = "sb"  # DOCKER_COMPOSE_PROJECT_NAME inside every process
FRONTEND = "frontend"
PORT = 8000


def slug(service_id):
    return service_id.lower().replace("_", "-")


def replica_name(service_id, n):
    return f"{PROJECT}-local-{slug(service_id)}-service-{n}"


def load_topology(path):
    with open(path) as f:
        cfg = json.load(f)
    graph = cfg.get("call_graph")
    if not graph or not graph.get("services"):
        raise SystemExit(f"{path}: call_graph.services is missing or empty")
    ids = [s["id"] for s in graph["services"]]
    slugs = [slug(i) for i in ids]
    if len(set(slugs)) != len(slugs):
        raise SystemExit(f"{path}: service ids collide after normalisation: {ids}")
    return cfg


def replicas_of(cfg):
    """[(service_id, replica_number, workload_name)] for every child replica."""
    out = []
    for s in cfg["call_graph"]["services"]:
        for n in range(1, int(s.get("replicas", 1)) + 1):
            out.append((s["id"], n, replica_name(s["id"], n)))
    return out


def cpus_per_replica(cfg):
    # Both placements occur in shipped topologies.
    v = cfg.get("child_cpus_per_replica", cfg["call_graph"].get("child_cpus_per_replica", 1))
    return v


# --------------------------------------------------------------------------- YAML

def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(str(v))  # JSON strings are valid YAML


def dump_yaml(obj, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.extend(dump_yaml(v, indent + 1))
            elif isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
    else:
        for item in obj:
            if isinstance(item, dict) and item:
                sub = dump_yaml(item, indent + 1)
                lines.append(f"{pad}- {sub[0].lstrip()}")
                lines.extend(sub[1:])
            elif isinstance(item, list):
                raise ValueError("nested lists unsupported")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return lines


def to_yaml(obj):
    return "\n".join(dump_yaml(obj)) + "\n"


# ------------------------------------------------------------------------ Compose

def _dot(rel):
    return rel if rel.startswith(".") else f"./{rel}"


IMAGE = "rpcstack-synthbench"
MODE_DEFAULT = "baseline"
CONF_DIR = "/etc/synthbench"  # compose mounts config/<name> here
LOAD_DIR = "/etc/synthbench-load"  # k8s mounts the loadgen ConfigMap here


def compose(cfg, config_rel, context="."):
    """Compose project: frontend, one service per child replica, client-bench."""
    cpus = cpus_per_replica(cfg)
    image = f"{IMAGE}:${{MODE:-{MODE_DEFAULT}}}"
    reps = replicas_of(cfg)
    common = {
        "image": image,
        "pull_policy": "never",
        "restart": "unless-stopped",
        "environment": {
            "LOG_LEVEL": "${LOG_LEVEL:-info}",
            "DOCKER_COMPOSE_PROJECT_NAME": PROJECT,
            "CPUS_PER_REPLICA": str(cpus),
            "MASA_FANOUT_AWARE": "${MASA_FANOUT_AWARE:-1}",
        },
        "volumes": [f"{_dot(config_rel)}:{CONF_DIR}:ro"],
    }
    limits = {"deploy": {"resources": {"limits": {"cpus": str(cpus)}}}}

    services = {
        FRONTEND: {
            **common,
            "build": {
                "context": context,
                "args": {
                    "MODE": f"${{MODE:-{MODE_DEFAULT}}}",
                    "FEATURES": "${FEATURES:-}",
                    "BUILD_JOBS": "${BUILD_JOBS:-}",
                },
            },
            "command": ["frontend"],
            "ports": [f"${{FRONTEND_PORT:-{PORT}}}:{PORT}"],
            "depends_on": {name: {"condition": "service_started"} for _, _, name in reps},
            **limits,
        }
    }
    for sid, _, name in reps:
        services[name] = {
            **common,
            "environment": {**common["environment"], "SERVICE_ID": sid},
            "command": ["child"],
            **limits,
        }
    # Load generator: runs once against the frontend and prints CSV traces.
    # Re-run with: docker compose run --rm client-bench
    services["client-bench"] = {
        "image": image,
        "pull_policy": "never",
        "restart": "no",
        "environment": {"LOG_LEVEL": "${LOG_LEVEL:-info}"},
        "volumes": [f"{_dot(config_rel)}:{CONF_DIR}:ro"],
        "command": ["client-bench"],
        "depends_on": {FRONTEND: {"condition": "service_started"}},
    }
    return {"services": services}


# ----------------------------------------------------------------------------- k8s

def k8s_docs(cfg, registry="", tag=MODE_DEFAULT, namespace=None):
    cpus = cpus_per_replica(cfg)
    prefix = f"{registry.rstrip('/')}/" if registry else ""
    label = {"app.kubernetes.io/part-of": "synthbench"}
    meta = lambda name: {  # noqa: E731
        "name": name,
        "labels": dict(label),
        **({"namespace": namespace} if namespace else {}),
    }
    cm_name = f"{PROJECT}-config"
    docs = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": meta(cm_name),
            "data": {"topology.json": json.dumps(cfg, indent=2) + "\n"},
        }
    ]

    def workload(name, role, extra_env):
        sel = {"app.kubernetes.io/name": name}
        env = [
            {"name": "LOG_LEVEL", "value": "info"},
            {"name": "DOCKER_COMPOSE_PROJECT_NAME", "value": PROJECT},
            {"name": "CPUS_PER_REPLICA", "value": str(cpus)},
            {"name": "MASA_FANOUT_AWARE", "value": "1"},
        ] + extra_env
        dep = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta(name),
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": sel},
                "template": {
                    "metadata": {"labels": {**label, **sel}},
                    "spec": {
                        "containers": [
                            {
                                "name": role,
                                "image": f"{prefix}{IMAGE}:{tag}",
                                "imagePullPolicy": "IfNotPresent",
                                "args": [role],
                                "env": env,
                                "ports": [{"containerPort": PORT, "name": "grpc"}],
                                "resources": {
                                    "limits": {"cpu": str(cpus)},
                                    "requests": {"cpu": "100m"},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": f"{CONF_DIR}/topology.json",
                                        "subPath": "topology.json",
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": cm_name}}],
                    },
                },
            },
        }
        svc = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": meta(name),
            "spec": {
                "selector": sel,
                "ports": [{"name": "grpc", "port": PORT, "targetPort": "grpc"}],
            },
        }
        return [dep, svc]

    for sid, _, name in replicas_of(cfg):
        docs += workload(name, "child", [{"name": "SERVICE_ID", "value": sid}])
    docs += workload(FRONTEND, "frontend", [])
    return docs


def k8s_loadgen_job(gen_configs, registry="", tag=MODE_DEFAULT, namespace=None):
    """One-shot load generator Job; `gen_configs` maps file name to config dict.

    The Job runs gen_config.json; scripts/smoke-k8s.sh swaps the
    SYNTHBENCH_GEN_CONFIG path to run another file from the same ConfigMap.
    """
    data = {}
    for name, gc in sorted(gen_configs.items()):
        gc = dict(gc)
        gc["Addr"] = f"http://{FRONTEND}:{PORT}"
        data[name] = json.dumps(gc, indent=2) + "\n"
    prefix = f"{registry.rstrip('/')}/" if registry else ""
    meta_ns = {"namespace": namespace} if namespace else {}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{PROJECT}-loadgen-config", **meta_ns},
            "data": data,
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": f"{PROJECT}-loadgen", **meta_ns},
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "client-bench",
                                "image": f"{prefix}{IMAGE}:{tag}",
                                "imagePullPolicy": "IfNotPresent",
                                "args": ["client-bench"],
                                "env": [
                                    {"name": "LOG_LEVEL", "value": "info"},
                                    {
                                        "name": "SYNTHBENCH_GEN_CONFIG",
                                        "value": f"{LOAD_DIR}/gen_config.json",
                                    },
                                ],
                                "volumeMounts": [{"name": "gen", "mountPath": LOAD_DIR}],
                            }
                        ],
                        "volumes": [
                            {"name": "gen", "configMap": {"name": f"{PROJECT}-loadgen-config"}}
                        ],
                    }
                },
            },
        },
    ]


def loadgen_configs(config_dir):
    """gen_config.json plus smoke/*.json; k8s ConfigMap keys cannot contain '/'."""
    out = {}
    with open(os.path.join(config_dir, "gen_config.json")) as f:
        out["gen_config.json"] = json.load(f)
    smoke = os.path.join(config_dir, "smoke")
    if os.path.isdir(smoke):
        for fn in sorted(os.listdir(smoke)):
            if fn.endswith(".json"):
                with open(os.path.join(smoke, fn)) as f:
                    out[f"smoke-{fn}"] = json.load(f)
    return out


def to_multidoc(docs):
    return "---\n".join(to_yaml(d) for d in docs)


def probe_targets(cfg):
    """`--target service::method=addr` for the first replica of every service."""
    out = []
    for s in cfg["call_graph"]["services"]:
        if s.get("methods"):
            out.append(
                f"--target {s['id']}::{s['methods'][0]['name']}="
                f"http://{replica_name(s['id'], 1)}:{PORT}"
            )
    return out


def render(args):
    """{filename: content} for one topology (config/<name>/topology.json)."""
    config_dir = os.path.dirname(os.path.abspath(args.topology))
    cfg = load_topology(args.topology)
    out = os.path.abspath(args.out)
    comp = compose(cfg, os.path.relpath(config_dir, out), os.path.relpath(REPO_ROOT, out))
    hdr = "# Generated by scripts/gen-deploy.py. Do not edit.\n"
    return {
        "docker-compose.yaml": hdr + to_yaml(comp),
        "k8s.yaml": hdr + to_multidoc(k8s_docs(cfg, args.registry, args.tag)),
        "k8s-loadgen.yaml": hdr
        + to_multidoc(k8s_loadgen_job(loadgen_configs(config_dir), args.registry, args.tag)),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("topology", help="config/<name>/topology.json")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY, help="image registry prefix for k8s")
    ap.add_argument("--tag", default=MODE_DEFAULT, help="image tag for k8s")
    ap.add_argument("--check", action="store_true", help="fail if existing outputs differ")
    ap.add_argument(
        "--probe-args",
        action="store_true",
        help="print --target arguments for synthbench_policy_probe and exit",
    )
    args = ap.parse_args(argv)

    if args.probe_args:
        print(" ".join(probe_targets(load_topology(args.topology))))
        return 0
    files = render(args)
    if args.check:
        stale = []
        for name, content in files.items():
            path = os.path.join(args.out, name)
            if not os.path.exists(path) or open(path).read() != content:
                stale.append(path)
        if stale:
            print(
                "stale generated files (rerun scripts/gen-all.sh): " + ", ".join(stale),
                file=sys.stderr,
            )
            return 1
        return 0
    os.makedirs(args.out, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(args.out, name), "w") as f:
            f.write(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
