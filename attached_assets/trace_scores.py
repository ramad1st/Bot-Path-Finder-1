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
_simulate_pick = bot._simulate_pick
_assess_post_move = bot._assess_post_move
_score_state   = bot._score_state
_get_avail_type_counts = bot._get_avail_type_counts
_min_blockers_for_type = bot._min_blockers_for_type
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
        
        singles = sum(1 for c in held.values() if c == 1)
        
        if held_size >= 4 and singles >= 3:
            avail = _get_available(pile)
            print(f"\n=== Step {step+1}: hand={held_size}/7 singles={singles} ===")
            print(f"  Hand: {dict(sorted(held.items()))}")
            
            all_moves = []
            for i in ix.iter_bits(avail):
                bt = ix.btype[i]
                in_hand = held.get(bt, 0)
                new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
                ok, penalty, analysis = _assess_post_move(pile, i, held, new_pile, new_held, new_size, matched)
                
                move_type = "MATCH" if matched else ("PAIR" if in_hand == 1 else ("TRIPLE" if in_hand == 2 else "NEW"))
                
                if not ok:
                    print(f"  REJECTED {move_type}(t={bt}) layer={ix.layer[i]}")
                else:
                    s = _score_state(new_pile, new_held, new_size) + penalty
                    all_moves.append((s, i, bt, move_type, in_hand))
            
            all_moves.sort(key=lambda x: x[0], reverse=True)
            for rank, (s, i, bt, mt, ih) in enumerate(all_moves[:8]):
                marker = " <<<" if rank == 0 else ""
                print(f"  #{rank+1} {mt:6s}(t={bt:2d}) score={s:10.0f} layer={ix.layer[i]}{marker}")
            
            pair_moves = [x for x in all_moves if x[3] == "PAIR"]
            new_moves = [x for x in all_moves if x[3] == "NEW"]
            if pair_moves and new_moves:
                best_pair = pair_moves[0][0]
                best_new = new_moves[0][0]
                print(f"  PAIR vs NEW gap: {best_pair - best_new:+.0f}")

        block_idx, reason = _beam_search(pile, held, held_size)
        if block_idx is None:
            print(f"\n=== DEADLOCK at step {step} ===")
            break
        block = ix.blocks[block_idx]
        ok, matched = gs.apply_touch(block["id"])
        if not ok:
            break
        step += 1

if __name__ == "__main__":
    with open("level_data.json") as f:
        data = json.load(f)
    trace(data)
