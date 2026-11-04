import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("gen_deploy", os.path.join(ROOT, "scripts/gen-deploy.py"))
gd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gd)


def topo(**svc_replicas):
    return {
        "call_graph": {
            "entry_points": {"a": [{"Front_End::run": 1.0}]},
            "services": [
                {"id": sid, "replicas": r, "methods": []} for sid, r in svc_replicas.items()
            ],
            "child_cpus_per_replica": 2.0,
        }
    }


class GenDeploy(unittest.TestCase):
    def test_hostnames_match_app_convention(self):
        # Must equal format!("{project}-local-{id lower, _->-}-service") + "-{n}" in the app.
        cfg = topo(Front_End=1, leaf=3)
        names = [n for _, _, n in gd.replicas_of(cfg)]
        self.assertEqual(
            names,
            [
                "sb-local-front-end-service-1",
                "sb-local-leaf-service-1",
                "sb-local-leaf-service-2",
                "sb-local-leaf-service-3",
            ],
        )

    def test_compose_one_service_per_replica(self):
        c = gd.compose(topo(Front_End=1, leaf=2), "config/t.json", "config/lg")
        s = c["services"]
        self.assertEqual(
            set(s),
            {"frontend", "sb-local-front-end-service-1", "sb-local-leaf-service-1",
             "sb-local-leaf-service-2", "loadgen", "probe"},
        )
        self.assertEqual(s["sb-local-leaf-service-2"]["environment"]["SERVICE_ID"], "leaf")
        self.assertEqual(s["frontend"]["environment"]["DOCKER_COMPOSE_PROJECT_NAME"], "sb")
        self.assertEqual(s["frontend"]["deploy"]["resources"]["limits"]["cpus"], "2.0")
        self.assertEqual(len(s["frontend"]["depends_on"]), 3)

    def test_k8s_service_per_replica_and_config(self):
        docs = gd.k8s_docs(topo(Front_End=1, leaf=2), registry="localhost:5001", tag="baseline")
        svcs = {d["metadata"]["name"] for d in docs if d["kind"] == "Service"}
        self.assertEqual(
            svcs,
            {"frontend", "sb-local-front-end-service-1", "sb-local-leaf-service-1",
             "sb-local-leaf-service-2"},
        )
        imgs = {d["spec"]["template"]["spec"]["containers"][0]["image"]
                for d in docs if d["kind"] == "Deployment"}
        self.assertEqual(imgs, {"localhost:5001/synthbench-child:baseline",
                                "localhost:5001/synthbench-frontend:baseline"})
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        self.assertEqual(json.loads(cm["data"]["config.json"])["call_graph"]["services"][1]["id"], "leaf")

    def test_loadgen_addr_rewritten_for_k8s(self):
        job = gd.k8s_loadgen_job({"gen_config.json": {"Addr": "http://x:1", "Rps": [1]}})
        cfg = json.loads(job[0]["data"]["gen_config.json"])
        self.assertEqual(cfg["Addr"], "http://frontend:8000")

    def test_probe_targets_use_first_replica_and_first_method(self):
        cfg = topo(Front_End=1, leaf=2)
        cfg["call_graph"]["services"][1]["methods"] = [{"name": "run"}, {"name": "x"}]
        self.assertEqual(gd.probe_targets(cfg), ["--target leaf::run=http://sb-local-leaf-service-1:8000"])

    def test_collision_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(topo(A_b=1, **{"a-b": 1}), f)
        with self.assertRaises(SystemExit):
            gd.load_topology(f.name)
        os.unlink(f.name)

    def test_yaml_roundtrip_shape(self):
        out = gd.to_yaml({"a": [{"b": 1, "c": ["x", "y"]}], "d": {}, "e": "q\"z"})
        self.assertEqual(out, 'a:\n  - b: 1\n    c:\n      - "x"\n      - "y"\nd: {}\ne: "q\\"z"\n')

    def test_checked_in_outputs_are_current(self):
        for name in sorted(os.listdir(os.path.join(ROOT, "config"))):
            subprocess.check_call([
                sys.executable, os.path.join(ROOT, "scripts/gen-deploy.py"),
                f"config/{name}/topology.json", "--loadgen-dir", f"config/{name}",
                "--out", f"deploy/{name}", "--registry", gd.DEFAULT_REGISTRY, "--check",
            ], cwd=ROOT)


if __name__ == "__main__":
    unittest.main()
