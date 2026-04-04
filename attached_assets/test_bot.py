"""
اختبار مستقل لمنطق CamelBot على بيانات حقيقية.

طريقة الاستخدام:
  python3 test_bot.py data.json
  أو
  python3 test_bot.py  (يعرض شكل الملف المطلوب)

⚠️  الملف optimized_bot_fixed.py يجب أن يكون في نفس المجلد.

شكل ملف JSON المطلوب:
{
  "pile_blocks": [
    {"id": 1, "col": 100, "row": 80, "layer": 0, "type": 5},
    ...
  ],
  "hand_blocks":    [],   (اختياري)
  "storage_blocks": []    (اختياري)
}
"""
from __future__ import annotations

import sys
import json
import types
import importlib
import time
from collections import defaultdict
from typing import Optional

# ---------------------------------------------------------------------------
# نحاكي mitmproxy حتى نستطيع استيراد الملف بدونه
# ---------------------------------------------------------------------------
def _mock_mitmproxy():
    http_mod = types.ModuleType("mitmproxy.http")
    ctx_mod  = types.ModuleType("mitmproxy.ctx")

    class _FakeHTTPFlow:
        pass

    http_mod.HTTPFlow = _FakeHTTPFlow  # type: ignore[attr-defined]

    mitmproxy_mod = types.ModuleType("mitmproxy")
    mitmproxy_mod.http = http_mod     # type: ignore[attr-defined]
    mitmproxy_mod.ctx  = ctx_mod      # type: ignore[attr-defined]

    sys.modules["mitmproxy"]      = mitmproxy_mod
    sys.modules["mitmproxy.http"] = http_mod
    sys.modules["mitmproxy.ctx"]  = ctx_mod

_mock_mitmproxy()

# ---------------------------------------------------------------------------
# استيراد المنطق من الملف المصحح
# ---------------------------------------------------------------------------
import importlib.util, pathlib

BOT_PATH = pathlib.Path(__file__).parent / "optimized_bot_fixed.py"
spec = importlib.util.spec_from_file_location("camelbot", BOT_PATH)
bot  = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
spec.loader.exec_module(bot)                   # type: ignore[union-attr]

_set_level      = bot._set_level
_beam_search    = bot._beam_search
_popcount       = bot._popcount
_get_available  = bot._get_available
_level_idx      = lambda: bot._level_idx
GameState       = bot.GameState
check_match     = bot.check_match
logger          = bot.logger


# ---------------------------------------------------------------------------
# تشغيل المحاكاة
# ---------------------------------------------------------------------------
def simulate(data: dict) -> None:
    pile_blocks    = data["pile_blocks"]
    hand_blocks    = data.get("hand_blocks",    [])
    storage_blocks = data.get("storage_blocks", [])

    _set_level(pile_blocks)
    ix = _level_idx()

    gs = GameState(ix, pile_blocks, hand_blocks, storage_blocks)

    print("=" * 60)
    print(f"المستوى | كتل={len(pile_blocks)} | يد ابتدائية={gs.hand_size()}")
    avail_start = _popcount(_get_available(gs.pile_mask))
    print(f"متاح في البداية={avail_start} | طبقات={max(b['layer'] for b in pile_blocks)}")
    print("=" * 60)

    step          = 0
    total_matches = 0
    frozen_reason = ""

    while True:
        if gs.is_won():
            print(f"\n✅ فزنا في {step} خطوة! ماتشات={total_matches}")
            break
        if gs.is_dead():
            print(f"\n❌ خسرنا! اليد امتلأت بعد {step} خطوة")
            break

        pile      = gs.pile_mask
        held      = gs.held_counts()
        held_size = gs.hand_size()

        t0 = time.time()
        block_idx, reason = _beam_search(pile, held, held_size)
        dt = time.time() - t0

        if block_idx is None:
            avail_left = _popcount(_get_available(pile))
            pile_left  = _popcount(pile)
            frozen_reason = reason
            print(f"\n🚫 توقّف البوت — {reason}")
            print(f"   بلوكات على اللوح={pile_left} | متاحة={avail_left} | يد={held_size}/7")
            print(f"   اليد: {dict(sorted(held.items()))}")
            break

        block = ix.blocks[block_idx]
        ok, matched = gs.apply_touch(block["id"])
        if not ok:
            print(f"\n⚠️  فشل تطبيق البلوك id={block['id']}")
            break

        step += 1
        if matched is not None:
            total_matches += 1

        pile_left = _popcount(gs.pile_mask)
        match_str = f" → ماتش! نوع={matched}" if matched is not None else ""
        tier_str  = ""

        print(
            f"خطوة {step:3d}: "
            f"نوع={block['type']:2d} | طبقة={block['layer']} | "
            f"يد={gs.hand_size()}/7 | باقي={pile_left} | "
            f"{dt*1000:.1f}ms{match_str}"
        )

    print("=" * 60)


# ---------------------------------------------------------------------------
# نقطة الدخول
# ---------------------------------------------------------------------------
import random as _random

_clear_caches = bot._clear_caches

def solve_fast(data: dict) -> tuple[int, bool]:
    """Run a single attempt, return (steps, won)."""
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    gs = GameState(ix, pile_blocks, [], [])
    step = 0
    while True:
        if gs.is_won(): return step, True
        if gs.is_dead(): return step, False
        pile = gs.pile_mask; held = gs.held_counts(); hs = gs.hand_size()
        bi, reason = _beam_search(pile, held, hs)
        if bi is None: return step, False
        ok, matched = gs.apply_touch(ix.blocks[bi]["id"])
        if not ok: return step, False
        step += 1


_simulate_pick = bot._simulate_pick
_assess_post_move = bot._assess_post_move
_score_state = bot._score_state
_min_blockers_for_type = bot._min_blockers_for_type
_get_unlocks = bot._get_unlocks
_get_depth_below = bot._get_depth_below
_uncover_score = bot._uncover_score


_get_avail_type_counts = bot._get_avail_type_counts

def _quick_score_v1(pile, held, held_size, i, ix):
    """Original quick_score heuristic."""
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    remaining = bot._get_pile_type_counts(new_pile)
    analysis = bot._analyze_held(new_held, remaining)
    if analysis.get("dead_pair_types"):
        return None, new_pile, new_held, new_size, matched
    s = _score_state(new_pile, new_held, new_size)
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    avail = _get_available(pile)
    avail_tc = {}
    for j in ix.iter_bits(avail):
        t = ix.btype[j]
        avail_tc[t] = avail_tc.get(t, 0) + 1
    visible = avail_tc.get(bt, 0)
    if ih == 0 and visible >= 3:
        s += 5200
    elif ih == 0 and visible == 2:
        s += 1200
    if matched is not None:
        s += 7000
    stuck_types = sum(1 for t, c in held.items()
                      if 0 < c < 3 and _min_blockers_for_type(pile, t) > 0)
    if ih == 1:
        avail_after = _get_available(new_pile)
        third = _popcount(avail_after & ix.type_mask.get(bt, 0))
        board_left_t = _popcount(new_pile & ix.type_mask.get(bt, 0))
        if third:
            pair_bonus = 8000 if stuck_types >= 3 else 3200
            s += pair_bonus
        elif board_left_t:
            mb_for_pair = _min_blockers_for_type(new_pile, bt)
            if mb_for_pair >= 3:
                s -= 5000
            elif mb_for_pair >= 2:
                s += 400
            else:
                s += 2500
        else:
            s -= 3500
    s += ix.layer[i] * 160
    s += _get_unlocks(pile, i) * 130
    s += _get_depth_below(pile, i) * 90
    pile_size = _popcount(pile)
    hard_board = pile_size > 150 or held_size >= 4
    unc_mult = 2.5 if hard_board else 1.5
    s += _uncover_score(pile, held, i) * unc_mult
    if analysis.get("dead_single_types"):
        s -= 5000
    return s, new_pile, new_held, new_size, matched


def _quick_score_v2(pile, held, held_size, i, ix):
    """Improved quick_score with better new-type and reachability handling."""
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    remaining = bot._get_pile_type_counts(new_pile)
    analysis = bot._analyze_held(new_held, remaining)
    if analysis.get("dead_pair_types"):
        return None, new_pile, new_held, new_size, matched
    s = _score_state(new_pile, new_held, new_size)
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    avail_after = _get_available(new_pile)
    avail_after_tc = {}
    for j in ix.iter_bits(avail_after):
        t = ix.btype[j]
        avail_after_tc[t] = avail_after_tc.get(t, 0) + 1
    if matched is not None:
        s += 7000
        avail_matches_after = sum(1 for t, c in new_held.items() 
                                   if c >= 1 and avail_after_tc.get(t, 0) >= (3 - c))
        s += avail_matches_after * 1500
    elif ih == 2:
        s += 7000
    elif ih == 1:
        third_avail = avail_after_tc.get(bt, 0)
        board_left_t = _popcount(new_pile & ix.type_mask.get(bt, 0))
        if third_avail:
            s += 5000
        elif board_left_t:
            mb = _min_blockers_for_type(new_pile, bt)
            if mb >= 3:
                s -= 4000
            elif mb >= 2:
                s -= 1000
            else:
                s += 1500
        else:
            s -= 5000
    elif ih == 0:
        avail_same_after = avail_after_tc.get(bt, 0)
        if avail_same_after >= 2:
            s += 3000
        elif avail_same_after == 1:
            mb = _min_blockers_for_type(new_pile, bt)
            s += 800 if mb <= 1 else -1500
        else:
            mb = _min_blockers_for_type(new_pile, bt)
            if mb >= 3:
                s -= 6000
            elif mb >= 2:
                s -= 3000
            else:
                s -= 500
        if held_size >= 4:
            s -= 2000
        if held_size >= 5:
            s -= 3000
    held_pair_completable = 0
    for t, c in new_held.items():
        a = avail_after_tc.get(t, 0)
        if c == 2 and a >= 1:
            held_pair_completable += 1
    s += held_pair_completable * 2000
    s += ix.layer[i] * 160
    s += _get_unlocks(pile, i) * 130
    s += _get_depth_below(pile, i) * 90
    pile_size = _popcount(pile)
    hard_board = pile_size > 150 or held_size >= 4
    unc_mult = 2.5 if hard_board else 1.5
    s += _uncover_score(pile, held, i) * unc_mult
    if analysis.get("dead_single_types"):
        s -= 5000
    return s, new_pile, new_held, new_size, matched


def _quick_score_fast(pile, held, held_size, i, ix):
    """Minimal fast scorer for mass restarts."""
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    new_pile = pile & ~ix.bit[i]
    new_held = dict(held)
    matched = None
    if ih == 2:
        del new_held[bt]
        new_size = held_size - 2
        matched = bt
    else:
        new_held[bt] = ih + 1
        new_size = held_size + 1
    
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    
    s = 0.0
    if matched is not None:
        s += 30000
    elif ih == 1:
        s += 10000
        avail_after = _get_available(new_pile)
        third = _popcount(avail_after & ix.type_mask.get(bt, 0))
        if third:
            s += 5000
    elif ih == 0:
        s -= 1000
    
    s += ix.layer[i] * 200
    s += _get_unlocks(pile, i) * 150
    s -= new_size * 1200
    
    if new_size >= 6:
        s -= 10000
    elif new_size >= 5:
        s -= 4000
    
    return s, new_pile, new_held, new_size, matched


def _quick_score_hybrid(pile, held, held_size, i, ix):
    """Use v1 when hand is low, v2 when hand is high."""
    if held_size >= 4:
        return _quick_score_v2(pile, held, held_size, i, ix)
    return _quick_score_v1(pile, held, held_size, i, ix)


def _quick_score_adaptive(pile, held, held_size, i, ix):
    """Adapts aggression based on hand size."""
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    
    remaining = bot._get_pile_type_counts(new_pile)
    analysis = bot._analyze_held(new_held, remaining)
    if analysis.get("dead_pair_types"):
        return None, new_pile, new_held, new_size, matched
    
    s = 0.0
    
    if matched is not None:
        s += 50000
        s += ix.layer[i] * 200
        s += _get_unlocks(pile, i) * 300
        return s, new_pile, new_held, new_size, matched
    
    if held_size >= 4:
        if ih == 1:
            avail_after = _get_available(new_pile)
            third_avail = _popcount(avail_after & ix.type_mask.get(bt, 0))
            if third_avail:
                s += 25000
            else:
                s -= 5000
        elif ih == 0:
            s -= 15000
            total_left = remaining.get(bt, 0)
            if total_left < 2:
                return None, new_pile, new_held, new_size, matched
        else:
            s += 10000
    else:
        if ih == 1:
            avail_after = _get_available(new_pile)
            third_avail = _popcount(avail_after & ix.type_mask.get(bt, 0))
            s += 15000
            if third_avail:
                s += 8000
        elif ih == 0:
            s -= 2000
        
    s += ix.layer[i] * 200
    s += _get_unlocks(pile, i) * 150
    s -= new_size * 1500
    
    if new_size >= 5:
        s -= 8000
    elif new_size >= 4:
        s -= 3000
    
    return s, new_pile, new_held, new_size, matched


def _quick_score_lookahead(pile, held, held_size, i, ix):
    """v1 scorer + depth-1 lookahead bonus."""
    result = _quick_score_v1(pile, held, held_size, i, ix)
    if result[0] is None:
        return result
    base_score, new_pile, new_held, new_size, matched = result
    
    avail2 = _get_available(new_pile)
    if not avail2:
        return result
    
    best_next = -999999
    for j in ix.iter_bits(avail2):
        r2 = _quick_score_v1(new_pile, new_held, new_size, j, ix)
        if r2[0] is not None and r2[0] > best_next:
            best_next = r2[0]
    
    if best_next > -999999:
        base_score += best_next * 0.3
    
    return base_score, new_pile, new_held, new_size, matched


def _quick_score_v4(pile, held, held_size, i, ix):
    """State-based: just evaluate the resulting state plus match bonus."""
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    remaining = bot._get_pile_type_counts(new_pile)
    analysis = bot._analyze_held(new_held, remaining)
    if analysis.get("dead_pair_types"):
        return None, new_pile, new_held, new_size, matched
    s = _score_state(new_pile, new_held, new_size)
    if matched is not None:
        s += 5000
    s += ix.layer[i] * 100
    s += _get_unlocks(pile, i) * 80
    return s, new_pile, new_held, new_size, matched


def _quick_score_v3(pile, held, held_size, i, ix):
    """Triple-completion focused heuristic."""
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    remaining = bot._get_pile_type_counts(new_pile)
    analysis = bot._analyze_held(new_held, remaining)
    if analysis.get("dead_pair_types"):
        return None, new_pile, new_held, new_size, matched
    
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    s = 0
    
    if matched is not None:
        s += 10000
        s += held_size * 500
    elif ih == 1:
        avail_after = _get_available(new_pile)
        third = _popcount(avail_after & ix.type_mask.get(bt, 0))
        if third:
            s += 8000
        else:
            mb = _min_blockers_for_type(new_pile, bt)
            if mb <= 1:
                s += 2000
            elif mb <= 2:
                s -= 500
            else:
                s -= 4000
    elif ih == 0:
        avail_after = _get_available(new_pile)
        same_avail = _popcount(avail_after & ix.type_mask.get(bt, 0))
        
        if same_avail >= 2:
            s += 5000
        elif same_avail == 1:
            mb3 = _min_blockers_for_type(new_pile, bt)
            s += 1500 if mb3 <= 1 else -1000
        else:
            mb = _min_blockers_for_type(new_pile, bt)
            if mb >= 3: s -= 8000
            elif mb >= 2: s -= 4000
            else: s -= 1000
        
        s -= held_size * 800
        distinct_types = len(new_held)
        if distinct_types >= 5: s -= 3000
        if distinct_types >= 6: s -= 5000
    
    avail_after = _get_available(new_pile)
    for t, c in new_held.items():
        if c == 2 and _popcount(avail_after & ix.type_mask.get(t, 0)):
            s += 3000
    
    s += ix.layer[i] * 200
    s += _get_unlocks(pile, i) * 150
    s += _get_depth_below(pile, i) * 80
    s += _uncover_score(pile, held, i) * 2.0
    
    if analysis.get("dead_single_types"):
        s -= 5000
    
    return s, new_pile, new_held, new_size, matched


_quick_score = _quick_score_v2


def _quick_score_fast(pile, held, held_size, i, ix):
    """Ultra-fast scorer for mutation search. Skips expensive analysis."""
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    s = -new_size * 550
    
    if matched is not None:
        s += 7000
    elif ih == 1:
        avail_after = _get_available(new_pile)
        third = _popcount(avail_after & ix.type_mask.get(bt, 0))
        if third:
            s += 5000
        else:
            board_left_t = _popcount(new_pile & ix.type_mask.get(bt, 0))
            if board_left_t:
                s += 500
            else:
                s -= 3000
    elif ih == 0:
        avail_after = _get_available(new_pile)
        same_avail = _popcount(avail_after & ix.type_mask.get(bt, 0))
        if same_avail >= 2:
            s += 3000
        elif same_avail == 1:
            s += 500
        else:
            s -= 2000
        if held_size >= 4:
            s -= 2000
    
    s += ix.layer[i] * 100
    s += _get_unlocks(pile, i) * 80
    
    return s, new_pile, new_held, new_size, matched


def solve_sa(data: dict, max_iters: int = 50000, verbose: bool = False) -> tuple[int, bool]:
    """Simulated annealing: optimize a tile priority ordering."""
    import random as _rand
    import math
    
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    tile_ids = []
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
            tile_ids.append(i)
    
    n_tiles = len(tile_ids)
    
    def simulate(priority_val):
        """Simulate game: at each step pick highest-priority available tile."""
        pile, held, hs = init_pile, {}, 0
        step = 0
        while True:
            if pile == 0 and hs == 0:
                return step, True
            if hs >= 7:
                return step, False
            avail = _get_available(pile)
            if not avail:
                return step, False
            
            best_i = -1
            best_pv = -1
            for i in ix.iter_bits(avail):
                bt = ix.btype[i]
                ih = held.get(bt, 0)
                if ih == 2:
                    pv = 1000000
                elif ih == 1:
                    pv = 500000 + priority_val[i]
                else:
                    pv = priority_val[i]
                if pv > best_pv:
                    best_pv = pv
                    best_i = i
            
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, best_i)
            if new_size >= 7 and matched is None:
                return step, False
            pile, held, hs = new_pile, new_held, new_size
            step += 1
    
    pv = {}
    for i in tile_ids:
        pv[i] = ix.layer[i] * 1000 + _get_unlocks(init_pile, i) * 100 + _rand.random()
    
    best_steps, best_won = simulate(pv)
    best_pv = dict(pv)
    current_steps = best_steps
    
    if best_won:
        if verbose: print(f"  ✅ SA WON on initial order ({best_steps} steps)")
        return best_steps, True
    
    temp = 5.0
    temp_decay = 0.99997
    
    for it in range(max_iters):
        i = _rand.choice(tile_ids)
        old_val = pv[i]
        pv[i] = _rand.gauss(old_val, 500)
        
        new_steps, new_won = simulate(pv)
        
        if new_won:
            if verbose: print(f"  ✅ SA WON at iter {it} ({new_steps} steps)")
            return new_steps, True
        
        delta = new_steps - current_steps
        if delta > 0 or _rand.random() < math.exp(delta / max(temp, 0.01)):
            current_steps = new_steps
            if new_steps > best_steps:
                best_steps = new_steps
                best_pv = dict(pv)
                if verbose and it % 5000 == 0:
                    print(f"  SA iter {it}: {best_steps} (temp={temp:.2f})")
        else:
            pv[i] = old_val
        
        temp *= temp_decay
    
    if verbose: print(f"  SA ({max_iters} iters): best={best_steps}")
    return best_steps, False


def solve_mutate(data: dict, num_mutations: int = 2000, scorer=None, verbose: bool = False) -> tuple[int, bool]:
    """Mutation search: replay best path, change one random decision, continue greedily."""
    if scorer is None:
        scorer = _quick_score_v1
    
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    import random as _rand
    
    def _greedy_rollout(pile, held, hs, scorer_fn):
        path = []
        step = 0
        while True:
            if pile == 0 and hs == 0:
                return step, True, path
            if hs >= 7:
                return step, False, path
            avail = _get_available(pile)
            if not avail:
                return step, False, path
            candidates = []
            for i in ix.iter_bits(avail):
                sc, np, nh, ns, m = scorer_fn(pile, held, hs, i, ix)
                if sc is not None:
                    candidates.append((sc, i))
            if not candidates:
                return step, False, path
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_i = candidates[0][1]
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, best_i)
            path.append((pile, dict(held), hs, best_i, candidates))
            pile, held, hs = new_pile, new_held, new_size
            step += 1
    
    base_steps, base_won, base_path = _greedy_rollout(init_pile, {}, 0, scorer)
    if base_won:
        if verbose: print(f"  ✅ WON on base rollout ({base_steps} steps)")
        return base_steps, True
    
    best_steps = base_steps
    best_path = list(base_path)
    
    for mut in range(num_mutations):
        if not best_path:
            break
        
        mutate_at = _rand.randint(0, len(best_path) - 1)
        
        old_pile, old_held, old_hs, old_choice, old_candidates = best_path[mutate_at]
        
        alt_choices = [(sc, i) for sc, i in old_candidates if i != old_choice]
        if not alt_choices:
            continue
        
        new_choice = _rand.choice(alt_choices[:5])[1]
        new_pile, new_held, new_size, matched = _simulate_pick(old_pile, dict(old_held), old_hs, new_choice)
        if new_size >= 7 and matched is None:
            continue
        
        remaining_steps, remaining_won, remaining_path = _greedy_rollout(new_pile, new_held, new_size, scorer)
        
        total = mutate_at + 1 + remaining_steps
        if remaining_won:
            if verbose: print(f"  ✅ WON by mutation at step {mutate_at} (total {total})")
            return total, True
        if total > best_steps:
            best_steps = total
            new_full_path = best_path[:mutate_at]
            new_full_path.append((old_pile, dict(old_held), old_hs, new_choice, old_candidates))
            new_full_path.extend(remaining_path)
            best_path = new_full_path
            if verbose and mut % 200 == 0: print(f"  mut#{mut}: {best_steps} steps (new best)")
    
    if verbose: print(f"  mutate ({num_mutations} tries): best={best_steps}")
    return best_steps, False


def _run_from_state(pile, held, hs, use_beam=False):
    """Run greedy forward from state, return (steps_taken, won)."""
    step = 0
    ix = _level_idx()
    while True:
        if pile == 0 and hs == 0:
            return step, True
        if hs >= 7:
            return step, False
        
        if use_beam:
            bi, reason = _beam_search(pile, held, hs)
            if bi is None:
                return step, False
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, bi)
        else:
            avail = _get_available(pile)
            if not avail:
                return step, False
            
            candidates = []
            for i in ix.iter_bits(avail):
                sc, np, nh, ns, m = _quick_score(pile, held, hs, i, ix)
                if sc is not None:
                    candidates.append((sc, i))
            
            if not candidates:
                return step, False
            
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_i = candidates[0][1]
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, best_i)
        
        pile, held, hs = new_pile, new_held, new_size
        step += 1


def solve_backtrack(data: dict, max_backtracks: int = 5000, branch_factor: int = 3, verbose: bool = False, max_stack: int = 30000) -> tuple[int, bool]:
    """Pure DFS from initial state with _quick_score heuristic and adaptive branching."""
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    stack = []
    best_steps = 0
    backtracks = 0
    
    pile, held, hs = init_pile, {}, 0
    step = 0
    
    while backtracks < max_backtracks:
        if pile == 0 and hs == 0:
            if verbose: print(f"  ✅ WON at step {step} (bt#{backtracks})")
            return step, True
        
        if hs >= 7:
            if step > best_steps:
                best_steps = step
                if verbose: print(f"  bt#{backtracks}: {step} steps (new best)")
            
            if not stack:
                break
            pile, held, hs, step, remaining = stack.pop()
            backtracks += 1
            choice_i = remaining[0]
            rest = remaining[1:]
            if rest:
                stack.append((pile, dict(held), hs, step, rest))
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, choice_i)
            pile, held, hs = new_pile, new_held, new_size
            step += 1
            continue
        
        avail = _get_available(pile)
        if not avail:
            if step > best_steps:
                best_steps = step
            if not stack:
                break
            pile, held, hs, step, remaining = stack.pop()
            backtracks += 1
            choice_i = remaining[0]
            rest = remaining[1:]
            if rest:
                stack.append((pile, dict(held), hs, step, rest))
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, choice_i)
            pile, held, hs = new_pile, new_held, new_size
            step += 1
            continue
        
        candidates = []
        for i in ix.iter_bits(avail):
            sc, np, nh, ns, m = _quick_score(pile, held, hs, i, ix)
            if sc is not None:
                candidates.append((sc, i))
        
        if not candidates:
            if step > best_steps:
                best_steps = step
            if not stack:
                break
            pile, held, hs, step, remaining = stack.pop()
            backtracks += 1
            choice_i = remaining[0]
            rest = remaining[1:]
            if rest:
                stack.append((pile, dict(held), hs, step, rest))
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, choice_i)
            pile, held, hs = new_pile, new_held, new_size
            step += 1
            continue
        
        candidates.sort(key=lambda x: x[0], reverse=True)
        
        if len(candidates) >= 2 and len(stack) < max_stack:
            bf = branch_factor if step >= 10 else 2
            alt_indices = [c[1] for c in candidates[1:bf]]
            if alt_indices:
                stack.append((pile, dict(held), hs, step, alt_indices))
        
        best_i = candidates[0][1]
        new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, best_i)
        pile, held, hs = new_pile, new_held, new_size
        step += 1
    
    if verbose: print(f"  {backtracks} backtracks, best={best_steps}")
    return best_steps, False


def solve_population_beam(data: dict, beam_width: int = 300, expand_top: int = 3, verbose: bool = False) -> tuple[int, bool]:
    """Population beam: maintain top-K states, expand top-N moves per state."""
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    population = [(0.0, init_pile, {}, 0)]
    best_steps = 0
    
    for step in range(226):
        if not population:
            break
        
        next_gen = []
        for state_score, pile, held, hs in population:
            if pile == 0 and hs == 0:
                if verbose: print(f"  ✅ WON at step {step}")
                return step, True
            if hs >= 7:
                continue
            
            avail = _get_available(pile)
            if not avail:
                continue
            
            candidates = []
            for i in ix.iter_bits(avail):
                new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, i)
                
                if new_size >= 7 and matched is None:
                    continue
                
                s = _score_state(new_pile, new_held, new_size)
                
                if matched is not None:
                    s += 7000
                
                bt = ix.btype[i]
                ih = held.get(bt, 0)
                if ih == 1:
                    avail_after = _get_available(new_pile)
                    third = _popcount(avail_after & ix.type_mask.get(bt, 0))
                    if third:
                        s += 4000
                
                s += ix.layer[i] * 100
                s += _get_unlocks(pile, i) * 80
                
                candidates.append((s, i, new_pile, dict(new_held), new_size))
            
            if not candidates:
                continue
            
            candidates.sort(key=lambda x: x[0], reverse=True)
            
            for s, i, np, nh, ns in candidates[:expand_top]:
                cumul = state_score + s
                next_gen.append((cumul, np, nh, ns))
        
        if not next_gen:
            break
        
        next_gen.sort(key=lambda x: x[0], reverse=True)
        
        seen = set()
        deduped = []
        for item in next_gen:
            key = (item[1], tuple(sorted(item[2].items())), item[3])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
                if len(deduped) >= beam_width:
                    break
        
        population = deduped
        
        if step > best_steps:
            best_steps = step
        
        if verbose and step % 50 == 0:
            print(f"  step {step}: pop={len(population)}")
    
    if verbose: print(f"  pop beam best: {best_steps} steps")
    return best_steps, False


def solve_hybrid(data: dict, max_backtracks: int = 5000, verbose: bool = False) -> tuple[int, bool]:
    """Beam search for first part, then DFS from various checkpoints."""
    pile_blocks = data["pile_blocks"]
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    bot._scoring_noise = 0.0
    bot._fast_mode = False
    bot._tabu_set = set()
    beam_states = [(init_pile, {}, 0)]
    pile, held, hs = init_pile, {}, 0
    beam_len = 0
    while True:
        if pile == 0 and hs == 0:
            return beam_len, True
        if hs >= 7:
            break
        bi, reason = _beam_search(pile, held, hs)
        if bi is None:
            break
        beam_len += 1
        new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, bi)
        pile, held, hs = new_pile, new_held, new_size
        beam_states.append((pile, dict(held), hs))
    
    if verbose: print(f"  beam: {beam_len} steps")
    best_steps = beam_len
    
    bt_per_cp = max(100, max_backtracks // 20)
    
    for cp_pct in [75, 60, 50, 40, 30, 20, 10, 0]:
        cp_step = max(0, beam_len * cp_pct // 100)
        if cp_step >= len(beam_states):
            continue
        cp_pile, cp_held, cp_hs = beam_states[cp_step]
        
        stack = []
        pile, held, hs = cp_pile, dict(cp_held), cp_hs
        step = cp_step
        backtracks = 0
        
        while backtracks < bt_per_cp:
            if pile == 0 and hs == 0:
                if verbose: print(f"  ✅ WON at step {step} from cp={cp_step} (bt#{backtracks})")
                return step, True
            
            if hs >= 7:
                if step > best_steps:
                    best_steps = step
                    if verbose: print(f"  cp={cp_step} bt#{backtracks}: {step} steps (new best)")
                if not stack:
                    break
                pile, held, hs, step, remaining = stack.pop()
                backtracks += 1
                choice_i = remaining[0]
                rest = remaining[1:]
                if rest:
                    stack.append((pile, dict(held), hs, step, rest))
                new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, choice_i)
                pile, held, hs = new_pile, new_held, new_size
                step += 1
                continue
            
            avail = _get_available(pile)
            if not avail:
                if step > best_steps:
                    best_steps = step
                if not stack:
                    break
                pile, held, hs, step, remaining = stack.pop()
                backtracks += 1
                choice_i = remaining[0]
                rest = remaining[1:]
                if rest:
                    stack.append((pile, dict(held), hs, step, rest))
                new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, choice_i)
                pile, held, hs = new_pile, new_held, new_size
                step += 1
                continue
            
            candidates = []
            for i in ix.iter_bits(avail):
                sc, np, nh, ns, m = _quick_score(pile, held, hs, i, ix)
                if sc is not None:
                    candidates.append((sc, i))
            
            if not candidates:
                if step > best_steps:
                    best_steps = step
                if not stack:
                    break
                pile, held, hs, step, remaining = stack.pop()
                backtracks += 1
                choice_i = remaining[0]
                rest = remaining[1:]
                if rest:
                    stack.append((pile, dict(held), hs, step, rest))
                new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, choice_i)
                pile, held, hs = new_pile, new_held, new_size
                step += 1
                continue
            
            candidates.sort(key=lambda x: x[0], reverse=True)
            
            bf = 3
            if len(candidates) >= 2 and len(stack) < 20000:
                alt_indices = [c[1] for c in candidates[1:bf]]
                if alt_indices:
                    stack.append((pile, dict(held), hs, step, alt_indices))
            
            best_i = candidates[0][1]
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, best_i)
            pile, held, hs = new_pile, new_held, new_size
            step += 1
    
    if verbose: print(f"  hybrid best: {best_steps}")
    return best_steps, False


def solve_tabu(data: dict, num_restarts: int = 200, verbose: bool = False) -> tuple[int, bool]:
    """Beam search with tabu: run beam, mark last few decisions as forbidden, re-run."""
    pile_blocks = data["pile_blocks"]
    best_steps = 0
    all_tabu = set()
    
    for restart in range(num_restarts):
        bot._scoring_noise = 0.0
        bot._fast_mode = False
        bot._tabu_set = set(all_tabu)
        _clear_caches()
        _set_level(pile_blocks)
        ix = _level_idx()
        
        init_pile = 0
        for b in pile_blocks:
            i = ix.id_to_idx.get(b["id"])
            if i is not None:
                init_pile |= ix.bit[i]
        
        pile, held, hs = init_pile, {}, 0
        step = 0
        history = []
        
        while True:
            if pile == 0 and hs == 0:
                if verbose: print(f"  ✅ WON at restart {restart} ({step} steps)")
                return step, True
            if hs >= 7:
                break
            
            bi, reason = _beam_search(pile, held, hs)
            if bi is None:
                break
            
            history.append((pile, bi))
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, bi)
            pile, held, hs = new_pile, new_held, new_size
            step += 1
        
        if step > best_steps:
            best_steps = step
            if verbose: print(f"  restart {restart}: {step} steps (new best)")
        
        if history:
            cutback = min(10, max(1, len(history) // 3))
            for k in range(max(0, len(history) - cutback), len(history)):
                all_tabu.add(history[k])
    
    if verbose: print(f"  tabu ({num_restarts} restarts): best={best_steps}")
    return best_steps, False


def solve_randomized(data: dict, num_attempts: int = 10000, verbose: bool = False) -> tuple[int, bool]:
    """Fast randomized greedy: at each step, randomly pick among top candidates."""
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    best_steps = 0
    import random as _rand
    
    for attempt in range(num_attempts):
        pile, held, hs = init_pile, {}, 0
        step = 0
        
        while True:
            if pile == 0 and hs == 0:
                if verbose: print(f"  ✅ WON at attempt {attempt} ({step} steps)")
                return step, True
            if hs >= 7:
                break
            
            avail = _get_available(pile)
            if not avail:
                break
            
            candidates = []
            for i in ix.iter_bits(avail):
                bt = ix.btype[i]
                ih = held.get(bt, 0)
                new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, i)
                
                if new_size >= 7 and matched is None:
                    continue
                
                if matched is not None:
                    tier = 3
                elif ih == 2:
                    tier = 3
                elif ih == 1:
                    tier = 2
                else:
                    tier = 1
                
                layer_bonus = ix.layer[i]
                unlock_bonus = _get_unlocks(pile, i)
                
                sc = tier * 10000 + layer_bonus * 160 + unlock_bonus * 130
                candidates.append((sc, tier, i, new_pile, new_held, new_size))
            
            if not candidates:
                break
            
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_tier = candidates[0][1]
            same_tier = [c for c in candidates if c[1] == best_tier]
            
            if best_tier == 3:
                choice = same_tier[0]
            else:
                choice = _rand.choice(same_tier[:min(5, len(same_tier))])
            
            _, _, bi, new_pile, new_held, new_size = choice
            pile, held, hs = new_pile, new_held, new_size
            step += 1
        
        if step > best_steps:
            best_steps = step
            if verbose and attempt % 2000 == 0:
                print(f"  attempt {attempt}: best={best_steps}")
    
    if verbose: print(f"  randomized ({num_attempts} tries): best={best_steps}")
    return best_steps, False


def solve_noisy_dfs(data: dict, scorer, max_backtracks: int = 500, noise: float = 500.0,
                    num_restarts: int = 15, verbose: bool = False) -> tuple[int, bool]:
    """Multiple DFS restarts with noise injection for diverse exploration."""
    global _quick_score
    _quick_score = scorer
    
    pile_blocks = data["pile_blocks"]
    best_steps = 0
    
    for restart in range(num_restarts):
        _clear_caches()
        _set_level(pile_blocks)
        ix = _level_idx()
        
        init_pile = 0
        for b in pile_blocks:
            i = ix.id_to_idx.get(b["id"])
            if i is not None:
                init_pile |= ix.bit[i]
        
        stack = [(init_pile, {}, 0, 0, [])]
        backtracks = 0
        local_best = 0
        
        while stack and backtracks < max_backtracks:
            pile, held, hs, step, tried = stack[-1]
            
            if pile == 0 and hs == 0:
                return step, True
            
            if step > local_best:
                local_best = step
            
            avail = _get_available(pile)
            if not avail:
                stack.pop()
                backtracks += 1
                continue
            
            candidates = []
            for i in ix.iter_bits(avail):
                if i in tried:
                    continue
                result = _quick_score(pile, held, hs, i, ix)
                sc = result[0]
                if sc is None:
                    continue
                sc += _random.gauss(0, noise)
                candidates.append((sc, i, result[1], result[2], result[3], result[4]))
            
            if not candidates:
                stack.pop()
                backtracks += 1
                continue
            
            candidates.sort(key=lambda x: x[0], reverse=True)
            sc, bi, new_pile, new_held, new_size, matched = candidates[0]
            
            tried.append(bi)
            stack.append((new_pile, new_held, new_size, step + 1, []))
        
        if local_best > best_steps:
            best_steps = local_best
    
    if verbose: print(f"  ndfs ({num_restarts}x{max_backtracks}bt n={noise:.0f}): best={best_steps}")
    return best_steps, False


def solve_checkpoint_restart(data: dict, scorer, noise: float = 300.0, 
                             checkpoint_interval: int = 15, num_restarts_per: int = 30,
                             verbose: bool = False) -> tuple[int, bool]:
    """Run greedy, save checkpoints, then restart from each checkpoint with noise."""
    global _quick_score
    _quick_score = scorer
    
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    pile, held, hs = init_pile, {}, 0
    checkpoints = [(0, pile, dict(held), hs)]
    step = 0
    while True:
        avail = _get_available(pile)
        if not avail:
            break
        candidates = []
        for i in ix.iter_bits(avail):
            result = _quick_score(pile, held, hs, i, ix)
            if result[0] is None:
                continue
            candidates.append(result)
        if not candidates:
            break
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, new_pile, new_held, new_size, matched = candidates[0]
        pile, held, hs = new_pile, new_held, new_size
        step += 1
        if step % checkpoint_interval == 0:
            checkpoints.append((step, pile, dict(held), hs))
        if pile == 0 and hs == 0:
            return step, True
    
    best_steps = step
    
    for cp_step, cp_pile, cp_held, cp_hs in reversed(checkpoints):
        if cp_step < best_steps - 30:
            break
        for restart in range(num_restarts_per):
            pile, held, hs = cp_pile, dict(cp_held), cp_hs
            cur_step = cp_step
            while True:
                if pile == 0 and hs == 0:
                    return cur_step, True
                avail = _get_available(pile)
                if not avail:
                    break
                candidates = []
                for i in ix.iter_bits(avail):
                    result = _quick_score(pile, held, hs, i, ix)
                    sc = result[0]
                    if sc is None:
                        continue
                    sc += _random.gauss(0, noise)
                    candidates.append((sc, i, result[1], result[2], result[3], result[4]))
                if not candidates:
                    break
                candidates.sort(key=lambda x: x[0], reverse=True)
                _, bi, new_pile, new_held, new_size, matched = candidates[0]
                pile, held, hs = new_pile, new_held, new_size
                cur_step += 1
            if cur_step > best_steps:
                best_steps = cur_step
    
    if verbose: print(f"  checkpoint restart: best={best_steps}")
    return best_steps, False


def solve_epsilon_greedy_dfs(data: dict, scorer, max_backtracks: int = 500, 
                             epsilon: float = 0.15, num_restarts: int = 20,
                             verbose: bool = False) -> tuple[int, bool]:
    """DFS with epsilon-greedy: with probability epsilon, pick random from top-3."""
    global _quick_score
    _quick_score = scorer
    
    pile_blocks = data["pile_blocks"]
    best_steps = 0
    
    for restart in range(num_restarts):
        _clear_caches()
        _set_level(pile_blocks)
        ix = _level_idx()
        
        init_pile = 0
        for b in pile_blocks:
            i = ix.id_to_idx.get(b["id"])
            if i is not None:
                init_pile |= ix.bit[i]
        
        stack = [(init_pile, {}, 0, 0, [])]
        backtracks = 0
        local_best = 0
        
        while stack and backtracks < max_backtracks:
            pile, held, hs, step, tried = stack[-1]
            
            if pile == 0 and hs == 0:
                return step, True
            
            if step > local_best:
                local_best = step
            
            avail = _get_available(pile)
            if not avail:
                stack.pop()
                backtracks += 1
                continue
            
            candidates = []
            for i in ix.iter_bits(avail):
                if i in tried:
                    continue
                result = _quick_score(pile, held, hs, i, ix)
                sc = result[0]
                if sc is None:
                    continue
                candidates.append((sc, i, result[1], result[2], result[3], result[4]))
            
            if not candidates:
                stack.pop()
                backtracks += 1
                continue
            
            candidates.sort(key=lambda x: x[0], reverse=True)
            if len(candidates) > 1 and _random.random() < epsilon:
                pick = _random.randint(0, min(3, len(candidates)) - 1)
            else:
                pick = 0
            sc, bi, new_pile, new_held, new_size, matched = candidates[pick]
            
            tried.append(bi)
            stack.append((new_pile, new_held, new_size, step + 1, []))
        
        if local_best > best_steps:
            best_steps = local_best
    
    if verbose: print(f"  eps DFS ({num_restarts}x{max_backtracks}bt e={epsilon}): best={best_steps}")
    return best_steps, False


def solve_mcts_rollout(data: dict, num_rollouts: int = 50, verbose: bool = False) -> tuple[int, bool]:
    """MCTS-like: at each step, evaluate moves by random rollout depth."""
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    pile, held, hs = init_pile, {}, 0
    step = 0
    
    while True:
        if pile == 0 and hs == 0:
            return step, True
        
        avail = _get_available(pile)
        if not avail:
            break
        
        avail_list = list(ix.iter_bits(avail))
        valid_moves = []
        for i in avail_list:
            new_pile, new_held, new_size, matched = _simulate_pick(pile, held, hs, i)
            if new_size >= 7 and matched is None:
                continue
            valid_moves.append((i, new_pile, new_held, new_size))
        
        if not valid_moves:
            break
        
        if len(valid_moves) == 1:
            _, pile, held, hs = valid_moves[0]
            step += 1
            continue
        
        best_move = None
        best_avg = -1
        
        for i, np, nh, ns in valid_moves:
            total_depth = 0
            for _ in range(num_rollouts):
                p, h, s = np, dict(nh), ns
                d = 0
                while True:
                    if p == 0 and s == 0:
                        d += 300
                        break
                    a = _get_available(p)
                    if not a:
                        break
                    al = list(ix.iter_bits(a))
                    valid = []
                    for j in al:
                        rp, rh, rs, rm = _simulate_pick(p, h, s, j)
                        if rs >= 7 and rm is None:
                            continue
                        valid.append((j, rp, rh, rs))
                    if not valid:
                        break
                    _, p, h, s = valid[_random.randint(0, len(valid) - 1)]
                    d += 1
                total_depth += d
            
            avg = total_depth / num_rollouts
            if avg > best_avg:
                best_avg = avg
                best_move = (i, np, nh, ns)
        
        if best_move is None:
            break
        
        _, pile, held, hs = best_move
        step += 1
        
        if verbose and step % 20 == 0:
            print(f"  step {step}: pile={_popcount(pile)}")
    
    if verbose: print(f"  mcts rollout: {step}")
    return step, False


def solve_noisy_greedy(data: dict, scorer, noise: float = 500.0,
                       num_restarts: int = 500, verbose: bool = False) -> tuple[int, bool]:
    """Pure greedy with noise injection, many fast restarts."""
    global _quick_score
    _quick_score = scorer
    
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()
    
    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]
    
    best_steps = 0
    
    for restart in range(num_restarts):
        pile, held, hs = init_pile, {}, 0
        step = 0
        
        while True:
            if pile == 0 and hs == 0:
                return step, True
            
            avail = _get_available(pile)
            if not avail:
                break
            
            candidates = []
            for i in ix.iter_bits(avail):
                result = _quick_score(pile, held, hs, i, ix)
                sc = result[0]
                if sc is None:
                    continue
                sc += _random.gauss(0, noise)
                candidates.append((sc, i, result[1], result[2], result[3], result[4]))
            
            if not candidates:
                break
            
            candidates.sort(key=lambda x: x[0], reverse=True)
            _, bi, new_pile, new_held, new_size, matched = candidates[0]
            pile, held, hs = new_pile, new_held, new_size
            step += 1
        
        if step > best_steps:
            best_steps = step
    
    if verbose: print(f"  noisy greedy ({num_restarts}x n={noise:.0f}): best={best_steps}")
    return best_steps, False


def solve_wide_beam(data: dict, beam_width: int = 30, scorer=None, verbose: bool = False) -> tuple[int, bool]:
    """Wide beam search keeping multiple live states."""
    if scorer is None:
        scorer = _quick_score_v1
    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()

    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]

    states = [(0.0, init_pile, {}, 0, 0)]
    best_steps = 0

    for round_num in range(226):
        next_states = []
        for cum_score, p, h, s, steps in states:
            if p == 0 and s == 0:
                return steps, True
            avail = _get_available(p)
            if not avail:
                if steps > best_steps:
                    best_steps = steps
                continue
            for i in ix.iter_bits(avail):
                r = scorer(p, h, s, i, ix)
                if r[0] is None:
                    continue
                next_states.append((cum_score + r[0], r[1], r[2], r[3], steps + 1))

        if not next_states:
            break

        next_states.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        filtered = []
        for ns in next_states:
            key = (ns[1], tuple(sorted(ns[2].items())), ns[3])
            if key in seen:
                continue
            seen.add(key)
            filtered.append(ns)
            if len(filtered) >= beam_width:
                break

        states = filtered
        cur_best = max(s[4] for s in states)
        if cur_best > best_steps:
            best_steps = cur_best

    if verbose:
        print(f"  wide beam (bw={beam_width}): {best_steps}")
    return best_steps, False


def solve_wide_beam_then_dfs(data: dict, beam_width: int = 30, scorer=None,
                              dfs_bt: int = 2000, verbose: bool = False) -> tuple[int, bool]:
    """Wide beam search followed by DFS from surviving states."""
    if scorer is None:
        scorer = _quick_score_v1
    global _quick_score
    _quick_score = scorer

    pile_blocks = data["pile_blocks"]
    _clear_caches()
    _set_level(pile_blocks)
    ix = _level_idx()

    init_pile = 0
    for b in pile_blocks:
        i = ix.id_to_idx.get(b["id"])
        if i is not None:
            init_pile |= ix.bit[i]

    states = [(0.0, init_pile, {}, 0, 0)]
    best_steps = 0
    all_terminal = []

    for round_num in range(226):
        next_states = []
        for cum_score, p, h, s, steps in states:
            if p == 0 and s == 0:
                return steps, True
            avail = _get_available(p)
            if not avail:
                if steps > best_steps:
                    best_steps = steps
                all_terminal.append((steps, p, h, s))
                continue
            has_valid = False
            for i in ix.iter_bits(avail):
                r = scorer(p, h, s, i, ix)
                if r[0] is None:
                    continue
                has_valid = True
                next_states.append((cum_score + r[0], r[1], r[2], r[3], steps + 1))
            if not has_valid:
                if steps > best_steps:
                    best_steps = steps
                all_terminal.append((steps, p, h, s))

        if not next_states:
            break

        next_states.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        filtered = []
        for ns in next_states:
            key = (ns[1], tuple(sorted(ns[2].items())), ns[3])
            if key in seen:
                continue
            seen.add(key)
            filtered.append(ns)
            if len(filtered) >= beam_width:
                break

        states = filtered
        cur_best = max(s[4] for s in states)
        if cur_best > best_steps:
            best_steps = cur_best

    all_terminal.sort(key=lambda x: x[0], reverse=True)
    bt_per = max(100, dfs_bt // max(1, len(all_terminal[:10])))
    for base_steps, p, h, s in all_terminal[:10]:
        if base_steps < best_steps - 10:
            continue
        s_dfs, w_dfs = _dfs_from_state(p, h, s, base_steps, scorer, bt_per, ix)
        if w_dfs:
            return s_dfs, True
        if s_dfs > best_steps:
            best_steps = s_dfs

    if verbose:
        print(f"  wide beam+DFS (bw={beam_width}): {best_steps}")
    return best_steps, False


def _dfs_from_state(pile, held, hs, base_steps, scorer, max_bt, ix):
    stack = [(pile, dict(held), hs, base_steps, [])]
    best = base_steps
    bt = 0
    while stack and bt < max_bt:
        p, h, s, step, tried = stack[-1]
        if p == 0 and s == 0:
            return step, True
        if step > best:
            best = step
        avail = _get_available(p)
        if not avail:
            stack.pop()
            bt += 1
            continue
        cands = []
        for i in ix.iter_bits(avail):
            if i in tried:
                continue
            r = scorer(p, h, s, i, ix)
            if r[0] is None:
                continue
            cands.append((r[0], i, r[1], r[2], r[3], r[4]))
        if not cands:
            stack.pop()
            bt += 1
            continue
        cands.sort(key=lambda x: x[0], reverse=True)
        sc, bi, np, nh, ns, m = cands[0]
        tried.append(bi)
        stack.append((np, nh, ns, step + 1, []))
    return best, False


def multi_attempt(data: dict, max_attempts: int = 5000, verbose: bool = True) -> tuple[int, bool]:
    """Combined: beam + DFS + wide beam + massive noisy DFS restarts."""
    import time as _time
    t0 = _time.time()
    time_budget = max(30, max_attempts * 0.01)

    bot._scoring_noise = 0.0
    bot._fast_mode = False
    bot._tabu_set = set()
    
    steps0, w0 = solve_fast(data)
    if w0:
        if verbose: print(f"  deterministic: ✅ WON in {steps0} steps!")
        return steps0, True
    if verbose: print(f"  beam: {steps0} steps")
    
    global _quick_score
    best = steps0
    
    for label, scorer in [("v1", _quick_score_v1), ("v2", _quick_score_v2)]:
        _quick_score = scorer
        bot._scoring_noise = 0.0
        bot._fast_mode = False
        bot._tabu_set = set()
        
        s_dfs, w_dfs = solve_backtrack(data, max_backtracks=3000, branch_factor=3, verbose=False)
        if w_dfs:
            if verbose: print(f"  ✅ {label} DFS")
            return s_dfs, True
        if s_dfs > best: best = s_dfs
        if verbose: print(f"  {label} DFS: {s_dfs}")
    
    s_wb, w_wb = solve_wide_beam_then_dfs(data, beam_width=30, scorer=_quick_score_v1, dfs_bt=500, verbose=False)
    if w_wb:
        if verbose: print(f"  ✅ wide beam")
        return s_wb, True
    if s_wb > best: best = s_wb
    if verbose: print(f"  wide beam: {s_wb}")
    
    scorers = [("v1", _quick_score_v1), ("v2", _quick_score_v2), ("hy", _quick_score_hybrid)]
    noise_levels = [200, 500, 1000, 2000, 3000, 5000]
    
    for label, scorer in scorers:
        ndfs_best = 0
        for noise_level in noise_levels:
            if _time.time() - t0 > time_budget:
                break
            restarts = max(10, max_attempts // (len(scorers) * len(noise_levels)))
            s_noisy, w_noisy = solve_noisy_dfs(data, scorer, max_backtracks=500, noise=noise_level, 
                                                num_restarts=restarts, verbose=False)
            if w_noisy:
                if verbose: print(f"  ✅ ndfs {label} n={noise_level}")
                return s_noisy, True
            if s_noisy > best: best = s_noisy
            if s_noisy > ndfs_best: ndfs_best = s_noisy
        if verbose: print(f"  ndfs {label}: {ndfs_best}")
    
    if verbose: print(f"  overall best: {best}")
    return best, False


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        mode = "single"
        if len(sys.argv) >= 3 and sys.argv[2] == "--multi":
            mode = "multi"
        
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
        
        if mode == "multi":
            attempts = int(sys.argv[3]) if len(sys.argv) >= 4 else 20
            print(f"Multi-attempt mode ({attempts} attempts)")
            steps, won = multi_attempt(data, attempts)
            if won:
                print(f"\n✅ فزنا! {steps} خطوة")
            else:
                print(f"\n❌ أفضل محاولة: {steps} خطوة")
        else:
            simulate(data)
    else:
        print("استخدم: python test_bot.py data.json [--multi [attempts]]")
        sys.exit(0)
