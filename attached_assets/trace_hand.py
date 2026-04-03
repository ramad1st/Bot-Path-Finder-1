from __future__ import annotations
import sys, json, types, importlib.util, pathlib

def _mock_mitmproxy():
    http_mod = types.ModuleType("mitmproxy.http")
    ctx_mod  = types.ModuleType("mitmproxy.ctx")
    class _FakeHTTPFlow: pass
    http_mod.HTTPFlow = _FakeHTTPFlow
    mitmproxy_mod = types.ModuleType("mitmproxy")
    mitmproxy_mod.http = http_mod
    mitmproxy_mod.ctx  = ctx_mod
    sys.modules["mitmproxy"]      = mitmproxy_mod
    sys.modules["mitmproxy.http"] = http_mod
    sys.modules["mitmproxy.ctx"]  = ctx_mod

_mock_mitmproxy()

BOT_PATH = pathlib.Path(__file__).parent / "optimized_bot_fixed.py"
spec = importlib.util.spec_from_file_location("camelbot", BOT_PATH)
bot  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

_set_level     = bot._set_level
_beam_search   = bot._beam_search
_popcount      = bot._popcount
_get_available = bot._get_available
GameState      = bot.GameState
_level_idx_fn  = lambda: bot._level_idx

def trace(data):
    pile_blocks = data["pile_blocks"]
    hand_blocks = data.get("hand_blocks", [])
    storage_blocks = data.get("storage_blocks", [])

    _set_level(pile_blocks)
    ix = _level_idx_fn()
    gs = GameState(ix, pile_blocks, hand_blocks, storage_blocks)

    step = 0

    while True:
        if gs.is_won() or gs.is_dead():
            break

        pile = gs.pile_mask
        held = gs.held_counts()
        held_size = gs.hand_size()

        block_idx, reason = _beam_search(pile, held, held_size)
        if block_idx is None:
            pile_left = _popcount(pile)
            avail_left = _popcount(_get_available(pile))
            singletons = sum(1 for v in held.values() if v == 1)
            pairs = sum(1 for v in held.values() if v == 2)
            print(f"\nStep {step:3d}: === DEADLOCK === pile={pile_left} avail={avail_left} hand={held_size}/7 singles={singletons} pairs={pairs}")
            print(f"  Hand: {dict(sorted(held.items()))}")
            
            avail_mask = _get_available(pile)
            avail_types = {}
            for i in range(225):
                if pile & (1 << i) and avail_mask & (1 << i):
                    bt = ix.btype[i]
                    avail_types[bt] = avail_types.get(bt, 0) + 1
            print(f"  Available types: {dict(sorted(avail_types.items()))}")
            
            matchable = []
            for bt, cnt in avail_types.items():
                need = 3 - held.get(bt, 0)
                if cnt >= need and need > 0:
                    matchable.append((bt, held.get(bt, 0), cnt))
            print(f"  Matchable from available: {matchable}")
            break

        block = ix.blocks[block_idx]
        bt = block["type"]
        
        ok, matched = gs.apply_touch(block["id"])
        if not ok:
            break
        step += 1
        
        new_held = gs.held_counts()
        new_size = gs.hand_size()
        singletons = sum(1 for v in new_held.values() if v == 1)
        distinct = len(new_held)
        
        action = ""
        if matched is not None:
            action = f"MATCH(t={matched})"
        elif held.get(bt, 0) == 0:
            action = f"NEW(t={bt})"
        elif held.get(bt, 0) == 1:
            action = f"PAIR(t={bt})"
        else:
            action = f"WAIT(t={bt})"

        pile_left = _popcount(gs.pile_mask)
        print(f"Step {step:3d}: {action:18s} hand={new_size}/7 singles={singletons} distinct={distinct} pile={pile_left} | {dict(sorted(new_held.items()))}")

if __name__ == "__main__":
    fname = sys.argv[1] if len(sys.argv) > 1 else "level_data.json"
    with open(fname) as f:
        data = json.load(f)
    print(f"=== {fname} ===")
    trace(data)
