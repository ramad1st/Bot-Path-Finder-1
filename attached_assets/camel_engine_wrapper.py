"""
Python wrapper for camel_engine.so - C-accelerated tile matching solver.
Drop-in replacement for _plan_solution in optimized_bot_fixed.py.
"""
import ctypes
import os
import json
import time

_dir = os.path.dirname(os.path.abspath(__file__))
_lib = ctypes.CDLL(os.path.join(_dir, "camel_engine.so"))

_lib.level_init.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
]
_lib.level_init.restype = None

_lib.plan_solution.argtypes = [
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
    ctypes.c_double,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
]
_lib.plan_solution.restype = ctypes.c_int

MAX_TYPES = 20
MAX_PATH = 230


def init_level(pile_blocks):
    blocks = sorted(pile_blocks, key=lambda b: b["id"])
    n = len(blocks)
    id_to_idx = {b["id"]: i for i, b in enumerate(blocks)}

    btypes = (ctypes.c_int * n)(*[b["type"] for b in blocks])
    layers = (ctypes.c_int * n)(*[b["layer"] for b in blocks])

    col = [b["col"] for b in blocks]
    row = [b["row"] for b in blocks]
    layer = [b["layer"] for b in blocks]
    X_SPAN = 2
    Y_SPAN = 2

    cb = [[0]*n for _ in range(n)]
    cv = [[0]*n for _ in range(n)]
    for i in range(n):
        ci, ri, li = col[i], row[i], layer[i]
        for j in range(i+1, n):
            if abs(ci - col[j]) < X_SPAN and abs(ri - row[j]) < Y_SPAN:
                lj = layer[j]
                if lj > li:
                    cb[i][j] = 1
                    cv[j][i] = 1
                elif li > lj:
                    cb[j][i] = 1
                    cv[i][j] = 1

    cb_flat = (ctypes.c_int * (n*n))()
    cv_flat = (ctypes.c_int * (n*n))()
    for i in range(n):
        for j in range(n):
            cb_flat[i*n+j] = cb[i][j]
            cv_flat[i*n+j] = cv[i][j]

    n_types = max(b["type"] for b in blocks)
    _lib.level_init(n, btypes, layers, cb_flat, cv_flat, n_types)
    return blocks, id_to_idx


def plan(held_dict, held_size, time_limit=15.0):
    init_held = (ctypes.c_int * (MAX_TYPES+1))()
    for t, c in held_dict.items():
        if 0 <= t <= MAX_TYPES:
            init_held[t] = c

    out_path = (ctypes.c_int * MAX_PATH)()
    out_len = ctypes.c_int(0)

    trial_count = _lib.plan_solution(
        init_held, held_size, ctypes.c_double(time_limit),
        out_path, ctypes.byref(out_len)
    )

    path = [out_path[i] for i in range(out_len.value)]
    return path, trial_count


if __name__ == "__main__":
    for level_num in range(1, 7):
        suffix = "" if level_num == 1 else f"_{level_num}"
        fname = os.path.join(_dir, f"level_data{suffix}.json")
        with open(fname) as fh:
            data = json.load(fh)
        pb = data["pile_blocks"]

        t0 = time.time()
        blocks, id_to_idx = init_level(pb)
        init_time = time.time() - t0

        t0 = time.time()
        path, trials = plan({}, 0, time_limit=15.0)
        plan_time = time.time() - t0

        print(f"L{level_num}: {len(path)} steps, {trials} trials, "
              f"init={init_time:.3f}s, plan={plan_time:.1f}s")
