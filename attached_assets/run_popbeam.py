import sys, types, importlib.util, pathlib, json, time

def _mock_mitmproxy():
    http_mod = types.ModuleType("mitmproxy.http")
    ctx_mod = types.ModuleType("mitmproxy.ctx")
    class _FakeHTTPFlow: pass
    http_mod.HTTPFlow = _FakeHTTPFlow
    mitmproxy_mod = types.ModuleType("mitmproxy")
    mitmproxy_mod.http = http_mod
    mitmproxy_mod.ctx = ctx_mod
    sys.modules["mitmproxy"] = mitmproxy_mod
    sys.modules["mitmproxy.http"] = http_mod
    sys.modules["mitmproxy.ctx"] = ctx_mod
_mock_mitmproxy()

BOT_PATH = pathlib.Path(__file__).parent / "optimized_bot_fixed.py"
spec = importlib.util.spec_from_file_location("camelbot", BOT_PATH)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

from test_bot import solve_population_beam, solve_fast

files = sys.argv[1:]
for f in files:
    data = json.load(open(f))
    bot._scoring_noise = 0.0
    bot._fast_mode = False
    bot._tabu_set = set()
    
    s0, w0 = solve_fast(data)
    
    t0 = time.time()
    s1, w1 = solve_population_beam(data, beam_width=200, verbose=False)
    dt = time.time() - t0
    
    best = max(s0, s1)
    status = "WON" if (w0 or w1) else f"{best}"
    print(f"{f}: beam={s0} pop={s1} best={status} ({dt:.1f}s)")
