import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts/lib/check_traces.py")
HDR = "api,request_id,slo,start_at,deadline,latency,error,q_lat_init,q_lat_resume,queue_lengths,hop_trace_json"


def run(errors, *flags):
    lines = ["noise", "---BEGIN TRACES---", "FILE: r1_a.csv", HDR]
    for i, e in enumerate(errors):
        lines.append(f'a,{i},1,{i * 1000},0,5,{e},0,0,"[1,2]","{{""k"": 1}}"')
    lines += ["---END FILE---", "---END TRACES---"]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(lines))
    try:
        return subprocess.run([sys.executable, SCRIPT, f.name, *flags], capture_output=True, text=True)
    finally:
        os.unlink(f.name)


class CheckTraces(unittest.TestCase):
    def test_ok_and_thresholds(self):
        r = run(["/None"] * 9 + ["/ClientMiss"], "--min-ok-rate", "0.99", "--max-transport", "0")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_transport_error_fails(self):
        r = run(["/None", "Unavailable: transport error"], "--max-transport", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("transport", r.stdout)

    def test_rejections(self):
        errs = ["/None", "/EarlyReturn?src=x::y"]
        self.assertEqual(run(errs, "--min-rejected", "1").returncode, 0)
        self.assertEqual(run(errs, "--max-rejected", "0").returncode, 1)

    def test_no_traces_fails(self):
        self.assertEqual(run([], "--min-requests", "1").returncode, 1)


if __name__ == "__main__":
    unittest.main()
