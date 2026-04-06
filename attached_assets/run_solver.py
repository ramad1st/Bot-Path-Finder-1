"""
CamelBot C Engine Solver
Usage: python3 run_solver.py [time_limit]

Example:
  python3 run_solver.py          # default 15 seconds per level
  python3 run_solver.py 10       # 10 seconds per level
"""
import sys
import json
import os
import time
import subprocess

_dir = os.path.dirname(os.path.abspath(__file__))

so_path = os.path.join(_dir, "camel_engine.so")
if not os.path.exists(so_path):
    print("Compiling C engine...")
    subprocess.check_call([
        "gcc", "-O3", "-march=native", "-shared", "-fPIC",
        "-o", so_path,
        os.path.join(_dir, "camel_engine.c"),
        "-lm"
    ])
    print("Done!")

import camel_engine_wrapper as cew

time_limit = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0

print(f"Running solver with {time_limit}s per level...\n")

total = 0
for lv in range(1, 7):
    suffix = "" if lv == 1 else f"_{lv}"
    fname = os.path.join(_dir, f"level_data{suffix}.json")
    with open(fname) as fh:
        data = json.load(fh)
    pb = data["pile_blocks"]

    cew.init_level(pb)
    t0 = time.time()
    path, trials = cew.plan({}, 0, time_limit=time_limit)
    elapsed = time.time() - t0

    total += len(path)
    print(f"  Level {lv}: {len(path)} moves  ({trials} trials, {elapsed:.1f}s)")

print(f"\n  TOTAL: {total} moves")
