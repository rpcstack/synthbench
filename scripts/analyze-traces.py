#!/usr/bin/env python3
"""Summarise and assert on load generator output (stdin or a file).

The loadgen prints each trace CSV between `FILE: <name>` and `---END FILE---`.
The `error` column classifies every request:

    /None            success within the SLO
    /ClientMiss      answered, but after the SLO
    /EarlyReturn...  rejected by the policy (deadline guard / admission)
    /ClientTimeout   no answer before the client timeout
    anything else    transport or application error ("unexpected")

Assertions (exit 1 on failure):
    --max-unexpected N        at most N unexpected errors (default 0)
    --min-success F           success fraction (/None) >= F
    --min-early-returns N     at least N policy rejections
    --min-requests N          at least N traced requests (default 1)
"""
import argparse
import csv
import io
import sys
from collections import Counter


def parse(text):
    rows, name, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("FILE: "):
            name, buf = line[6:].strip(), []
        elif line.startswith("---END FILE---") and name:
            rows.extend(csv.DictReader(io.StringIO("\n".join(buf))))
            name = None
        elif name:
            buf.append(line)
    return rows


def classify(error):
    if error == "/None":
        return "success"
    if error == "/ClientMiss":
        return "slo_miss"
    if error.startswith("/EarlyReturn"):
        return "early_return"
    if error == "/ClientTimeout":
        return "timeout"
    return "unexpected"


def summarise(rows):
    c = Counter(classify(r["error"]) for r in rows)
    c["total"] = len(rows)
    return c


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", nargs="?", help="loadgen output (default stdin)")
    ap.add_argument("--max-unexpected", type=int, default=0)
    ap.add_argument("--min-success", type=float)
    ap.add_argument("--min-early-returns", type=int)
    ap.add_argument("--min-requests", type=int, default=1)
    ap.add_argument("--label", default="load")
    args = ap.parse_args(argv)

    text = open(args.file).read() if args.file else sys.stdin.read()
    rows = parse(text)
    c = summarise(rows)
    total = c["total"]
    frac = c["success"] / total if total else 0.0
    print(
        f"[{args.label}] total={total} success={c['success']} ({frac:.3f}) "
        f"slo_miss={c['slo_miss']} early_return={c['early_return']} "
        f"timeout={c['timeout']} unexpected={c['unexpected']}"
    )
    bad = [r["error"] for r in rows if classify(r["error"]) == "unexpected"]
    if bad:
        print("  unexpected errors, first few: " + "; ".join(sorted(set(bad))[:3]))
    failures = []
    if total < args.min_requests:
        failures.append(f"only {total} traced requests, need {args.min_requests}")
    if c["unexpected"] > args.max_unexpected:
        failures.append(f"{c['unexpected']} unexpected errors (max {args.max_unexpected})")
    if args.min_success is not None and frac < args.min_success:
        failures.append(f"success fraction {frac:.3f} < {args.min_success}")
    if args.min_early_returns is not None and c["early_return"] < args.min_early_returns:
        failures.append(f"{c['early_return']} early returns < {args.min_early_returns}")
    for f in failures:
        print(f"  FAIL: {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
