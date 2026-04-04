import sys, types, importlib.util, pathlib, json, time, random

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

orig_score_state = bot._score_state

def make_tuned_score(single_pen, hs_mult, hs4_pen, hs5_pen):
    def _tuned_score_state(pile, held, held_size):
        cache_key = (pile, bot._held_key(held))
        v = bot._score_cache.get(cache_key)
        if v is not None:
            return v

        if held_size >= 7:
            return -200000.0
        if held_size >= 6:
            avail_now = bot._get_avail_type_counts(pile)
            completable = sum(1 for t, c in held.items() if c == 2 and avail_now.get(t, 0) > 0)
            if completable > 0: return -45000.0
            has_pair = any(c == 2 for c in held.values())
            if has_pair: return -70000.0
            return -90000.0

        score = 0.0
        remaining = bot._get_pile_type_counts(pile)
        analysis = bot._analyze_held(held, remaining)

        pair_rank = 0
        for count in sorted(held.values(), reverse=True):
            if count == 2:
                pair_rank += 1
                if pair_rank == 1: score += 1600
                elif pair_rank == 2: score += 250
                else: score -= 2200
            elif count == 1:
                score -= single_pen

        if analysis["dead_pair_types"]:
            score -= 9000 * len(analysis["dead_pair_types"])
        if analysis["dead_single_types"]:
            score -= 2200 * len(analysis["dead_single_types"])
        if analysis["open_incomplete_types"] >= 3:
            score -= 2800 * (analysis["open_incomplete_types"] - 2)

        avail_types = bot._get_avail_type_counts(pile)
        for btype, count in held.items():
            total_remaining = remaining.get(btype, 0)
            avail_now = avail_types.get(btype, 0)
            if count == 2:
                if total_remaining <= 0: score -= 5000
                elif avail_now > 0: score += 2600
                else: score += 700
            elif count == 1:
                if total_remaining < 2: score -= 1200
                elif avail_now >= 2: score += 500
                elif avail_now == 1: score += 120
                else: score -= 200

        score -= held_size * hs_mult
        if held_size >= 5:
            score -= hs5_pen
        elif held_size >= 4:
            score -= hs4_pen

        pile_size = bot._popcount(pile)
        avail_count = bot._popcount(bot._get_available(pile))
        blocked = max(pile_size - avail_count, 0)
        score += min(blocked, 40) * 15

        bot._score_cache[cache_key] = score
        return score
    return _tuned_score_state

files = ["level_data.json", "level_data_2.json", "level_data_3.json", 
         "level_data_4.json", "level_data_5.json", "level_data_6.json"]
levels = [json.load(open(f)) for f in files]

configs = [
    (450, 550, 2500, 6500, "original"),
    (700, 700, 3500, 8000, "conservative"),
    (300, 400, 2000, 5000, "aggressive"),
    (600, 800, 4000, 10000, "very_conservative"),
    (450, 550, 5000, 12000, "hs_heavy"),
    (900, 550, 2500, 6500, "single_heavy"),
    (200, 550, 2500, 6500, "single_light"),
    (450, 1000, 2500, 6500, "hs_mult_heavy"),
]

for single_pen, hs_mult, hs4_pen, hs5_pen, label in configs:
    bot._score_state = make_tuned_score(single_pen, hs_mult, hs4_pen, hs5_pen)
    results = []
    for i, data in enumerate(levels):
        bot._scoring_noise = 0.0
        bot._fast_mode = False
        bot._tabu_set = set()
        _clear_caches()
        s, w = solve_fast(data)
        results.append(s)
    total = sum(results)
    print(f"{label:20s}: {'/'.join(str(r) for r in results)} = {total}")

bot._score_state = orig_score_state
