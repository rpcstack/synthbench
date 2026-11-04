import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("at", os.path.join(ROOT, "scripts/analyze-traces.py"))
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)

HDR = "api,request_id,slo,start_at,deadline,latency,error,q_lat_init,q_lat_resume,queue_lengths,hop_trace_json"


def sample(errors):
    lines = ["noise", "FILE: r1_a.csv", HDR]
    for i, e in enumerate(errors):
        lines.append(f'a,{i},1,0,0,5,{e},0,0,"[1,2]","{{""k"": 1}}"')
    lines += ["---END FILE---", "---END TRACES---"]
    return "\n".join(lines)


class Analyze(unittest.TestCase):
    def test_classification(self):
        rows = at.parse(sample(["/None", "/None", "/ClientMiss", "/EarlyReturn?src=x", "/ClientTimeout", "transport error"]))
        c = at.summarise(rows)
        self.assertEqual(
            (c["total"], c["success"], c["slo_miss"], c["early_return"], c["timeout"], c["unexpected"]),
            (6, 2, 1, 1, 1, 1),
        )

    def test_assertions(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(sample(["/None"] * 9 + ["/ClientMiss"]))
        self.assertEqual(at.main([f.name, "--min-success", "0.9"]), 0)
        self.assertEqual(at.main([f.name, "--min-success", "0.95"]), 1)
        self.assertEqual(at.main([f.name, "--min-early-returns", "1"]), 1)
        os.unlink(f.name)

    def test_unexpected_fails_by_default(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(sample(["/None", "Unavailable: transport error"]))
        self.assertEqual(at.main([f.name]), 1)
        os.unlink(f.name)

    def test_empty_output_fails(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("no traces here")
        self.assertEqual(at.main([f.name]), 1)
        os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
