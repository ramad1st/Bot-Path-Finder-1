import sys, json, types, importlib.util, pathlib, random

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

from test_bot import solve_fast, _clear_caches

data = json.load(open("level_data.json"))

for noise in [100, 500, 1000, 2000, 5000]:
    best = 0
    for attempt in range(500):
        bot._scoring_noise = float(noise)
        bot._fast_mode = True
        bot._tabu_set = set()
        _clear_caches()
        s, w = solve_fast(data)
        if w:
            print(f"  noise={noise}: WON at attempt {attempt}!")
            break
        if s > best:
            best = s
    else:
        print(f"  noise={noise}: best={best} over 500 tries")
