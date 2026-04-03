import sys, json, unittest.mock
sys.modules['mitmproxy'] = unittest.mock.MagicMock()
sys.modules['mitmproxy.http'] = unittest.mock.MagicMock()
import importlib
bot = importlib.import_module("optimized_bot_fixed")

def analyze_level(fname):
    with open(fname) as f:
        data = json.load(f)
    
    pile_blocks = data["pile_blocks"]
    bot._init_level_index(pile_blocks)
    ix = bot._level_idx
    
    pile = 0
    for b in pile_blocks:
        pile |= ix.bit[b["id"]]
    
    held = {}
    held_size = 0
    total = bot._popcount(pile)
    step = 0
    
    all_moments = []
    
    while True:
        avail = bot._get_available(pile)
        if avail == 0:
            break
        
        pairs_in_hand = {t: c for t, c in held.items() if c == 2}
        pair_status = {}
        for t in pairs_in_hand:
            mb = bot._min_blockers_for_type(pile, t)
            board_left = bot._popcount(pile & ix.type_mask.get(t, 0))
            pair_status[t] = {"mb": mb, "left": board_left}
        
        move, reason = bot._select_move(pile, held, held_size)
        if move is None:
            break
        
        old_size = held_size
        pile, held, held_size, matched = bot._simulate_pick(pile, held, held_size, move)
        cleared = total - bot._popcount(pile) - held_size
        step += 1
        
        all_moments.append({
            "step": step, "cleared": cleared,
            "old_hs": old_size, "new_hs": held_size,
            "picked_type": ix.btype[move],
            "matched": matched is not None,
            "matched_type": ix.btype[matched] if matched is not None else None,
            "hand": dict(held),
            "pairs_status": pair_status,
            "reason": reason,
        })
    
    final_cleared = total - bot._popcount(pile) - held_size
    print(f"\n{'='*70}")
    print(f"Level: {fname}")
    print(f"Final cleared: {final_cleared}, Board: {bot._popcount(pile)}, Hand: {held_size}")
    print(f"Final hand: {dict(held)}")
    
    # Find the point where things go wrong - trace backwards from the end
    # Look for the last match and what happens after
    last_match_idx = -1
    for idx, m in enumerate(all_moments):
        if m['matched']:
            last_match_idx = idx
    
    print(f"\nLast match at step {all_moments[last_match_idx]['step']} (cleared={all_moments[last_match_idx]['cleared']})")
    print(f"\n--- Moves from 10 before last match to end ---")
    start_idx = max(0, last_match_idx - 10)
    for m in all_moments[start_idx:]:
        match_str = f" MATCH(t{m['matched_type']})" if m['matched'] else ""
        ps = ""
        if m['pairs_status']:
            ps = " pairs:{" + ",".join(f"t{t}:mb{v['mb']}" for t,v in m['pairs_status'].items()) + "}"
        print(f"  step{m['step']:3d} clr={m['cleared']:3d} hs={m['old_hs']}→{m['new_hs']} pick=t{m['picked_type']:2d}{match_str} hand={m['hand']}{ps}")
    
    # Count how many types have 3+ on board but stuck (min_blockers >= 3)
    print(f"\n--- Stuck state: types still completable on board ---")
    remaining_types = {}
    for i in ix.iter_bits(pile):
        t = ix.btype[i]
        remaining_types[t] = remaining_types.get(t, 0) + 1
    for t in sorted(remaining_types.keys()):
        c = remaining_types[t]
        if c >= 3:
            mb = bot._min_blockers_for_type(pile, t)
            avail_now = bot._popcount(bot._get_available(pile) & ix.type_mask.get(t, 0))
            print(f"  Type {t:2d}: on_board={c}, avail={avail_now}, min_blockers={mb}")
    
    # Analyze hand types
    print(f"\n--- Hand types vs board ---")
    for t, c in sorted(held.items()):
        board_left = bot._popcount(pile & ix.type_mask.get(t, 0))
        mb = bot._min_blockers_for_type(pile, t) if board_left > 0 else 999
        avail_now = bot._popcount(bot._get_available(pile) & ix.type_mask.get(t, 0))
        print(f"  Type {t:2d}: in_hand={c}, board_left={board_left}, avail={avail_now}, min_blockers={mb}")

for f in ["level_data.json", "level_data_5.json", "level_data_6.json"]:
    analyze_level(f)
