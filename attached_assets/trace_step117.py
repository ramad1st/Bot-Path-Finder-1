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
    sys.modules["mitmproxy"] = mitmproxy_mod
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

def trace(data):
    pile_blocks = data["pile_blocks"]
    _set_level(pile_blocks)
    ix = bot._level_idx
    gs = GameState(ix, pile_blocks, [], [])

    step = 0
    while True:
        if gs.is_won() or gs.is_dead():
            break
        pile = gs.pile_mask
        held = gs.held_counts()
        held_size = gs.hand_size()
        
        if step >= 90:
            avail = _get_available(pile)
            avail_types = {}
            for i in range(len(ix.blocks)):
                if pile & ix.bit[i] and avail & ix.bit[i]:
                    bt = ix.btype[i]
                    avail_types[bt] = avail_types.get(bt, 0) + 1
            
            can_pair = [bt for bt in held if held[bt] == 1 and avail_types.get(bt, 0) >= 1]
            can_triple = [bt for bt in held if held[bt] == 2 and avail_types.get(bt, 0) >= 1]
            new_only = [bt for bt, c in avail_types.items() if bt not in held]
            
            print(f"\n--- Before step {step+1} (hand={held_size}/7) ---")
            print(f"  Hand: {dict(sorted(held.items()))}")
            print(f"  Avail types: {dict(sorted(avail_types.items()))}")
            print(f"  Can pair (1→2): {can_pair}")
            print(f"  Can match (2→3): {can_triple}")
            print(f"  Only new types: {new_only}")

        block_idx, reason = _beam_search(pile, held, held_size)
        if block_idx is None:
            print(f"\n=== DEADLOCK at step {step} ===")
            break

        block = ix.blocks[block_idx]
        bt = block["type"]
        prev_count = held.get(bt, 0)
        ok, matched = gs.apply_touch(block["id"])
        if not ok:
            break
        step += 1
        
        if step >= 91:
            if matched is not None:
                act = f"MATCH(t={bt})"
            elif prev_count == 0:
                act = f"NEW(t={bt})"
            elif prev_count == 1:
                act = f"PAIR(t={bt})"
            else:
                act = f"TRIPLE(t={bt})"
            print(f"  >>> {act}")

if __name__ == "__main__":
    with open("level_data.json") as f:
        data = json.load(f)
    trace(data)
