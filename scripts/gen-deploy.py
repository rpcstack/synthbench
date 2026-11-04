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
IMAGES = {
    "frontend": "synthbench-frontend",
    "child": "synthbench-child",
    "loadgen": "synthbench-loadgen",
}


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


def compose(cfg, topology_rel, gen_config_rel=None, context="."):
    cpus = cpus_per_replica(cfg)
    base_env = {
        "LOG_LEVEL": "${LOG_LEVEL:-info}",
        "DOCKER_COMPOSE_PROJECT_NAME": PROJECT,
        "CPUS_PER_REPLICA": str(cpus),
        "MASA_FANOUT_AWARE": "${MASA_FANOUT_AWARE:-1}",
    }
    mounts = [f"{_dot(topology_rel)}:/usr/config.json:ro"]
    limits = {"deploy": {"resources": {"limits": {"cpus": str(cpus)}}}}
    reps = replicas_of(cfg)

    services = {
        FRONTEND: {
            "image": f"{IMAGES['frontend']}:${{SYNTHBENCH_MODE:-masa}}",
            "build": {
                "context": context,
                "target": "frontend",
                "args": {"MODE": "${SYNTHBENCH_MODE:-masa}"},
            },
            "environment": {**base_env, "BINARY_NAME": "synthbench_frontend"},
            "volumes": mounts,
            "depends_on": [name for _, _, name in reps],
            "networks": ["synthbench"],
            **limits,
        }
    }
    for sid, _, name in reps:
        services[name] = {
            "image": f"{IMAGES['child']}:${{SYNTHBENCH_MODE:-masa}}",
            "build": {
                "context": context,
                "target": "child",
                "args": {"MODE": "${SYNTHBENCH_MODE:-masa}"},
            },
            "environment": {**base_env, "BINARY_NAME": "synthbench_child", "SERVICE_ID": sid},
            "volumes": mounts,
            "networks": ["synthbench"],
            **limits,
        }
    if gen_config_rel:
        services["loadgen"] = {
            "image": f"{IMAGES['loadgen']}:${{SYNTHBENCH_MODE:-masa}}",
            "build": {
                "context": context,
                "target": "loadgen",
                "args": {"MODE": "${SYNTHBENCH_MODE:-masa}"},
            },
            "environment": {
                "LOG_LEVEL": "${LOG_LEVEL:-info}",
                "BINARY_NAME": "synthbench_client_bench",
            },
            "volumes": [f"{_dot(gen_config_rel)}:/usr/gen_config.json:ro"],
            "depends_on": [FRONTEND],
            "restart": "no",
            "networks": ["synthbench"],
        }
    return {"services": services, "networks": {"synthbench": {}}}


# ----------------------------------------------------------------------------- k8s

def k8s_docs(cfg, registry="", tag="masa", namespace=None):
    cpus = cpus_per_replica(cfg)
    prefix = f"{registry.rstrip('/')}/" if registry else ""
    label = {"app.kubernetes.io/part-of": "synthbench"}
    meta = lambda name, extra=None: {  # noqa: E731
        "name": name,
        "labels": {**label, **(extra or {})},
        **({"namespace": namespace} if namespace else {}),
    }
    cm_name = f"{PROJECT}-config"
    docs = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": meta(cm_name),
            "data": {"config.json": json.dumps(cfg, indent=2) + "\n"},
        }
    ]

    def workload(name, binary, image, extra_env, resources):
        sel = {"app.kubernetes.io/name": name}
        env = [
            {"name": "BINARY_NAME", "value": binary},
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
                                "name": "app",
                                "image": f"{prefix}{image}:{tag}",
                                "imagePullPolicy": "IfNotPresent",
                                "env": env,
                                "ports": [{"containerPort": PORT, "name": "grpc"}],
                                "resources": resources,
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/usr/config.json",
                                        "subPath": "config.json",
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

    res = {"limits": {"cpu": str(cpus)}, "requests": {"cpu": "100m"}}
    for sid, _, name in replicas_of(cfg):
        docs += workload(name, "synthbench_child", IMAGES["child"], [{"name": "SERVICE_ID", "value": sid}], res)
    docs += workload(FRONTEND, "synthbench_frontend", IMAGES["frontend"], [], res)
    return docs


def k8s_loadgen_job(gen_config, registry="", tag="masa", namespace=None):
    """One-shot load generator Job. gen_config Addr is rewritten to the frontend Service."""
    gc = dict(gen_config)
    gc["Addr"] = f"http://{FRONTEND}:{PORT}"
    prefix = f"{registry.rstrip('/')}/" if registry else ""
    meta_ns = {"namespace": namespace} if namespace else {}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{PROJECT}-loadgen-config", **meta_ns},
            "data": {"gen_config.json": json.dumps(gc, indent=2) + "\n"},
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
                                "name": "loadgen",
                                "image": f"{prefix}{IMAGES['loadgen']}:{tag}",
                                "imagePullPolicy": "IfNotPresent",
                                "env": [
                                    {"name": "BINARY_NAME", "value": "synthbench_client_bench"},
                                    {"name": "LOG_LEVEL", "value": "info"},
                                ],
                                "volumeMounts": [
                                    {
                                        "name": "gen",
                                        "mountPath": "/usr/gen_config.json",
                                        "subPath": "gen_config.json",
                                    }
                                ],
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


def to_multidoc(docs):
    return "---\n".join(to_yaml(d) for d in docs)


def render(args):
    """{filename: content} for one topology."""
    cfg = load_topology(args.topology)
    out = os.path.abspath(args.out)
    rel = lambda p: os.path.relpath(os.path.abspath(p), out)  # noqa: E731
    comp = compose(
        cfg,
        rel(args.topology),
        rel(args.gen_config) if args.gen_config else None,
        os.path.relpath(REPO_ROOT, out),
    )
    hdr = "# Generated by scripts/gen-deploy.py. Do not edit.\n"
    files = {
        "docker-compose.yaml": hdr + to_yaml(comp),
        "k8s.yaml": hdr + to_multidoc(k8s_docs(cfg, args.registry, args.tag)),
    }
    if args.gen_config:
        with open(args.gen_config) as g:
            job = k8s_loadgen_job(json.load(g), args.registry, args.tag)
        files["k8s-loadgen.yaml"] = hdr + to_multidoc(job)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("topology")
    ap.add_argument("--gen-config", help="loadgen config JSON (adds the loadgen service/job)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY, help="image registry prefix for k8s, e.g. localhost:5001")
    ap.add_argument("--tag", default="masa", help="image tag for k8s")
    ap.add_argument("--check", action="store_true", help="fail if existing outputs differ")
    args = ap.parse_args(argv)

    files = render(args)
    if args.check:
        stale = []
        for name, content in files.items():
            path = os.path.join(args.out, name)
            if not os.path.exists(path) or open(path).read() != content:
                stale.append(path)
        if stale:
            print("stale generated files (rerun scripts/gen-all.sh): " + ", ".join(stale), file=sys.stderr)
            return 1
        return 0
    os.makedirs(args.out, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(args.out, name), "w") as f:
            f.write(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
