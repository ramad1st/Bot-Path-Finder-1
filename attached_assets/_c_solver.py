import sys, json
sys.path.insert(0, sys.argv[1])
import camel_engine_wrapper as w
data = json.loads(sys.stdin.read())
w.init_level(data["pile_blocks"])
held = {int(k): v for k, v in data["held"].items()}
path, trials = w.plan(held, data["held_size"], time_limit=data["time_limit"])
print(json.dumps({"path": path, "trials": trials}))
