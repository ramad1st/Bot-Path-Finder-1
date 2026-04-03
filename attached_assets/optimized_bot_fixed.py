"""
Optimized CamelBot — faster and more accurate tile-matching bot.

Key optimizations over the original:
1. LevelIndex: precomputed spatial overlap graph (O(1) coverage checks)
2. Bitmask pile representation (O(1) state copy, O(1) cache keys)
3. Accurate unlock/reveal computations replace FAST_MODE approximations
4. Adaptive search depth (2-5) instead of fixed depth 2
5. Increased beam width (16 vs 12)
6. Reduced memory allocations in hot loops

Bug fixes (v2):
FIX-3: _beam_search Phase 3 — smart tiered emergency fallback.
        When ALL normal + risky_fallback moves are rejected but available moves
        still exist on the board, the bot no longer freezes.
        Priority tiers (safest first):
          T1 — immediate match (always safe, clears hand space)
          T2 — finish_next=True after pick (guaranteed escape next step)
          T3 — held stays <=4 (safe zone, max unlocks wins tie)
          T4 — last resort: penalises held>=5 heavily so 6/7 is chosen
               only when literally nothing else exists.
        Hard exclusions in all tiers: dead_pair or held>=7.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import threading
import time
from collections import defaultdict
from typing import Optional

from mitmproxy import http
from mitmproxy import ctx

SECRET       = "a3aabfe14ae1e5c7afe0a6d5b9c7c150"
UID          = 398487653
SEND_DELAY   = 0
BEAM_WIDTH   = 16
SEARCH_DEPTH = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("CamelBot")

_STOP = object()

X_SPAN = 20
Y_SPAN = 16

ENDGAME_PILE_LIMIT = 9
ENDGAME_SAFE_HELD  = 4

# ---------------------------------------------------------------------------
#  Popcount helper
# ---------------------------------------------------------------------------
try:
    (0).bit_count()
    def _popcount(x: int) -> int:
        return x.bit_count()
except AttributeError:
    def _popcount(x: int) -> int:
        return bin(x).count("1")


# ---------------------------------------------------------------------------
#  LevelIndex — built once per level, immutable after construction
# ---------------------------------------------------------------------------
class LevelIndex:
    """Pre-computed spatial relationships for every block in a level."""

    __slots__ = (
        "blocks", "n", "id_to_idx",
        "col", "row", "layer", "btype", "bit",
        "covered_by", "covers", "type_mask",
    )

    def __init__(self, pile_blocks: list[dict]) -> None:
        self.blocks: list[dict] = sorted(pile_blocks, key=lambda b: b["id"])
        self.n: int = len(self.blocks)
        self.id_to_idx: dict[int, int] = {b["id"]: i for i, b in enumerate(self.blocks)}

        col  = [b["col"]   for b in self.blocks]
        row  = [b["row"]   for b in self.blocks]
        self.col   = col
        self.row   = row
        self.layer = [b["layer"] for b in self.blocks]
        self.btype = [b["type"]  for b in self.blocks]
        self.bit   = [1 << i for i in range(self.n)]

        # covered_by[i] = bitmask of blocks ABOVE i that overlap it
        # covers[i]     = bitmask of blocks BELOW i that it overlaps (i covers them)
        cb = [0] * self.n
        cv = [0] * self.n
        for i in range(self.n):
            ci, ri, li = col[i], row[i], self.layer[i]
            for j in range(i + 1, self.n):
                if abs(ci - col[j]) < X_SPAN and abs(ri - row[j]) < Y_SPAN:
                    lj = self.layer[j]
                    if lj > li:
                        cb[i] |= 1 << j
                        cv[j] |= 1 << i
                    elif li > lj:
                        cb[j] |= 1 << i
                        cv[i] |= 1 << j
        self.covered_by = cb
        self.covers     = cv

        # type -> bitmask of blocks with that type
        tm: dict[int, int] = defaultdict(int)
        for i in range(self.n):
            tm[self.btype[i]] |= 1 << i
        self.type_mask: dict[int, int] = dict(tm)

    # ---- fast primitives ---------------------------------------------------

    def available_mask(self, pile: int) -> int:
        """Bitmask of blocks in *pile* that are not covered by any other block in *pile*."""
        avail = pile
        cb = self.covered_by
        rem = pile
        while rem:
            bit = rem & -rem
            idx = bit.bit_length() - 1
            if cb[idx] & pile:
                avail ^= bit
            rem ^= bit
        return avail

    def count_unlocks(self, pile: int, idx: int) -> int:
        """How many blocks become *available* when *idx* is removed from *pile*."""
        new_pile = pile ^ self.bit[idx]
        below = self.covers[idx] & pile
        cb = self.covered_by
        count = 0
        while below:
            bit = below & -below
            j = bit.bit_length() - 1
            if not (cb[j] & new_pile):
                count += 1
            below ^= bit
        return count

    def depth_below(self, pile: int, idx: int) -> int:
        """Distinct layer count among overlapping blocks below *idx* still in *pile*."""
        below = self.covers[idx] & pile
        if not below:
            return 0
        layers = set()
        lay = self.layer
        while below:
            bit = below & -below
            layers.add(lay[bit.bit_length() - 1])
            below ^= bit
        return len(layers)

    def type_counts(self, mask: int) -> dict[int, int]:
        """Per-type popcount within *mask*."""
        counts: dict[int, int] = {}
        for t, tmask in self.type_mask.items():
            c = _popcount(mask & tmask)
            if c:
                counts[t] = c
        return counts

    def reveals_strong_target(
        self, pile: int, idx: int, held: dict[int, int],
    ) -> bool:
        """Does removing *idx* reveal a block whose type we already hold (or
        create a visible triple of the same type)?"""
        new_pile = pile ^ self.bit[idx]
        below = self.covers[idx] & pile
        cb = self.covered_by
        bt = self.btype

        revealed_types: set[int] = set()
        while below:
            bit = below & -below
            j = bit.bit_length() - 1
            if not (cb[j] & new_pile):
                t = bt[j]
                if held.get(t, 0) >= 1:
                    return True
                revealed_types.add(t)
            below ^= bit

        if not revealed_types:
            return False

        # check if any revealed type now has ≥3 available (board-only triple)
        new_avail = self.available_mask(new_pile)
        tm = self.type_mask
        for t in revealed_types:
            if _popcount(new_avail & tm.get(t, 0)) >= 3:
                return True
        return False

    def iter_bits(self, mask: int):
        while mask:
            bit = mask & -mask
            yield bit.bit_length() - 1
            mask ^= bit


# ---------------------------------------------------------------------------
#  Global caches — keyed by integer bitmask (O(1) hash)
# ---------------------------------------------------------------------------
_level_idx: Optional[LevelIndex] = None

_avail_cache:       dict[int, int]                        = {}
_type_counts_cache: dict[int, dict[int, int]]             = {}
_avail_tc_cache:    dict[int, dict[int, int]]             = {}
_score_cache:       dict[tuple[int, tuple], float]        = {}
_unlock_cache:      dict[tuple[int, int], int]            = {}
_depth_cache:       dict[tuple[int, int], int]            = {}


def _clear_caches() -> None:
    _avail_cache.clear()
    _type_counts_cache.clear()
    _avail_tc_cache.clear()
    _score_cache.clear()
    _unlock_cache.clear()
    _depth_cache.clear()


def _set_level(pile_blocks: list[dict]) -> None:
    global _level_idx
    _level_idx = LevelIndex(pile_blocks)
    _clear_caches()


# ---- cached wrappers -------------------------------------------------------

def _get_available(pile: int) -> int:
    v = _avail_cache.get(pile)
    if v is not None:
        return v
    v = _level_idx.available_mask(pile)  # type: ignore[union-attr]
    _avail_cache[pile] = v
    return v


def _get_pile_type_counts(pile: int) -> dict[int, int]:
    v = _type_counts_cache.get(pile)
    if v is not None:
        return v
    v = _level_idx.type_counts(pile)  # type: ignore[union-attr]
    _type_counts_cache[pile] = v
    return v


def _get_avail_type_counts(pile: int) -> dict[int, int]:
    v = _avail_tc_cache.get(pile)
    if v is not None:
        return v
    avail = _get_available(pile)
    v = _level_idx.type_counts(avail)  # type: ignore[union-attr]
    _avail_tc_cache[pile] = v
    return v


def _get_unlocks(pile: int, idx: int) -> int:
    key = (pile, idx)
    v = _unlock_cache.get(key)
    if v is not None:
        return v
    v = _level_idx.count_unlocks(pile, idx)  # type: ignore[union-attr]
    _unlock_cache[key] = v
    return v


def _get_depth_below(pile: int, idx: int) -> int:
    key = (pile, idx)
    v = _depth_cache.get(key)
    if v is not None:
        return v
    v = _level_idx.depth_below(pile, idx)  # type: ignore[union-attr]
    _depth_cache[key] = v
    return v


# ---------------------------------------------------------------------------
#  Held-state helpers  (held = dict[type → count], very small ~5 entries)
# ---------------------------------------------------------------------------

def _held_key(held: dict[int, int]) -> tuple:
    return tuple(sorted(held.items()))


def _simulate_pick(
    pile: int,
    held: dict[int, int],
    held_size: int,
    idx: int,
) -> tuple[int, dict[int, int], int, Optional[int]]:
    """Return (new_pile, new_held, new_size, matched_type | None)."""
    ix = _level_idx  # type: ignore[union-attr]
    new_pile = pile ^ ix.bit[idx]
    btype = ix.btype[idx]

    new_held = dict(held)
    new_held[btype] = new_held.get(btype, 0) + 1
    new_size = held_size + 1
    matched: Optional[int] = None

    # resolve all triples
    changed = True
    while changed:
        changed = False
        for t, c in list(new_held.items()):
            if c >= 3:
                new_held[t] = c - 3
                if new_held[t] == 0:
                    del new_held[t]
                new_size -= 3
                matched = t
                changed = True
                break

    return new_pile, new_held, new_size, matched


def _will_complete(btype: int, held: dict[int, int]) -> bool:
    return held.get(btype, 0) >= 2


def _has_immediate_finish(pile: int, held: dict[int, int]) -> bool:
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    for i in ix.iter_bits(avail):
        if _will_complete(ix.btype[i], held):
            return True
    return False


def _finish_in_two(pile: int, held: dict[int, int], held_size: int) -> bool:
    """هل يمكن إكمال ثلاثية خلال خطوتين من الآن؟ (بدون تجاوز 6 في اليد)"""
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    for i in ix.iter_bits(avail):
        np1, nh1, ns1, m1 = _simulate_pick(pile, held, held_size, i)
        if m1 is not None:
            return True                          # ماتش في خطوة واحدة
        if ns1 >= 7:
            continue
        avail2 = _get_available(np1)
        for j in ix.iter_bits(avail2):
            _, _, ns2, m2 = _simulate_pick(np1, nh1, ns1, j)
            if m2 is not None and ns2 < 7:
                return True                      # ماتش في خطوتين
    return False


def _post_match_viable(pile: int, held: dict[int, int], held_size: int) -> bool:
    """هل الحالة بعد الماتش آمنة؟ يكفي أن يكون في اليد نوع واحد متاح فوراً"""
    if held_size <= 3:
        return True  # يد صغيرة = آمنة دائماً
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    # نبحث: هل أي نوع في اليد موجود في المتاح الآن أو في خطوتين؟
    for t, c in held.items():
        if c == 0:
            continue
        if _popcount(avail & ix.type_mask.get(t, 0)) > 0:
            return True  # نوع متاح فوراً
    return _finish_in_two(pile, held, held_size)


def _finish_in_three(pile: int, held: dict[int, int], held_size: int) -> bool:
    """هل يمكن إكمال ثلاثية خلال 3 خطوات وتظل الحالة بعدها قابلة للاستمرار؟"""
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    for i in ix.iter_bits(avail):
        np1, nh1, ns1, m1 = _simulate_pick(pile, held, held_size, i)
        if m1 is not None:
            # تحقق: الحالة بعد الماتش مستمرة؟
            if _post_match_viable(np1, nh1, ns1):
                return True
            continue
        if ns1 >= 7:
            continue
        avail2 = _get_available(np1)
        for j in ix.iter_bits(avail2):
            np2, nh2, ns2, m2 = _simulate_pick(np1, nh1, ns1, j)
            if m2 is not None and ns2 < 7:
                if _post_match_viable(np2, nh2, ns2):
                    return True
                continue
            if ns2 >= 7:
                continue
            avail3 = _get_available(np2)
            for k in ix.iter_bits(avail3):
                np3, nh3, ns3, m3 = _simulate_pick(np2, nh2, ns2, k)
                if m3 is not None and ns3 < 7:
                    # في 3 خطوات نقبل حتى بدون فحص post-match (بعيد كفاية)
                    return True
    return False


# ---------------------------------------------------------------------------
#  Analysis & scoring  (logic preserved from original, data structures faster)
# ---------------------------------------------------------------------------

def _analyze_held(
    held: dict[int, int],
    remaining: dict[int, int],
) -> dict:
    dead_pair: list[int] = []
    dead_single: list[int] = []
    pair_count = single_count = 0
    completable_pairs = completable_singles = 0
    open_incomplete = 0

    for t, c in held.items():
        if c <= 0:
            continue
        if c < 3:
            open_incomplete += 1
        rem = remaining.get(t, 0)
        if c == 2:
            pair_count += 1
            if rem <= 0:
                dead_pair.append(t)
            else:
                completable_pairs += 1
        elif c == 1:
            single_count += 1
            if rem < 2:
                dead_single.append(t)
            else:
                completable_singles += 1
        elif c + rem < 3:
            (dead_pair if c >= 2 else dead_single).append(t)

    return {
        "dead_pair_types": dead_pair,
        "dead_single_types": dead_single,
        "pair_count": pair_count,
        "single_count": single_count,
        "completable_pairs": completable_pairs,
        "completable_singles": completable_singles,
        "open_incomplete_types": open_incomplete,
    }


def _violates_endgame(
    new_pile: int,
    new_held: dict[int, int],
    new_size: int,
) -> bool:
    if _popcount(new_pile) > ENDGAME_PILE_LIMIT:
        return False
    if new_size <= ENDGAME_SAFE_HELD:
        return False

    remaining = _get_pile_type_counts(new_pile)
    analysis = _analyze_held(new_held, remaining)

    if new_size <= 5 and analysis["completable_pairs"] >= 1 and not analysis["dead_pair_types"]:
        return False
    return True


def _score_state(pile: int, held: dict[int, int], held_size: int) -> float:
    cache_key = (pile, _held_key(held))
    v = _score_cache.get(cache_key)
    if v is not None:
        return v

    if held_size >= 7:
        return -200000.0
    if held_size >= 6:
        return -90000.0

    score = 0.0
    remaining = _get_pile_type_counts(pile)
    analysis = _analyze_held(held, remaining)

    pair_rank = 0
    for count in sorted(held.values(), reverse=True):
        if count == 2:
            pair_rank += 1
            if pair_rank == 1:
                score += 1600
            elif pair_rank == 2:
                score += 250
            else:
                score -= 2200
        elif count == 1:
            score -= 450

    if analysis["dead_pair_types"]:
        score -= 9000 * len(analysis["dead_pair_types"])
    if analysis["dead_single_types"]:
        score -= 2200 * len(analysis["dead_single_types"])
    if analysis["open_incomplete_types"] >= 3:
        score -= 2800 * (analysis["open_incomplete_types"] - 2)

    avail_types = _get_avail_type_counts(pile)

    for btype, count in held.items():
        total_remaining = remaining.get(btype, 0)
        avail_now = avail_types.get(btype, 0)
        if count == 2:
            if total_remaining <= 0:
                score -= 5000
            elif avail_now > 0:
                score += 2600
            else:
                score += 700
        elif count == 1:
            if total_remaining < 2:
                score -= 1200
            elif avail_now >= 2:
                score += 500
            elif avail_now == 1:
                score += 120
            else:
                score -= 200

    score -= held_size * 550
    if held_size >= 5:
        score -= 6500
    elif held_size >= 4:
        score -= 2500

    pile_size = _popcount(pile)
    avail_count = _popcount(_get_available(pile))
    blocked = max(pile_size - avail_count, 0)
    score += min(blocked, 40) * 15

    _score_cache[cache_key] = score
    return score


# ---------------------------------------------------------------------------
#  Adaptive depth — key accuracy improvement (was always 2)
# ---------------------------------------------------------------------------

def _adaptive_depth(pile_size: int, held_size: int) -> int:
    if held_size >= 6:
        return 4          # critical — deep search for survival
    if pile_size <= 20:
        return 5          # endgame — near-exhaustive
    if pile_size <= 40:
        return 4
    if held_size >= 4:
        return 3          # danger zone
    if pile_size <= 80:
        return 3
    if pile_size <= 130:
        return 2
    return 2


# ---------------------------------------------------------------------------
#  assess_post_move  (safety filter + penalty, logic matches original)
# ---------------------------------------------------------------------------

def _assess_post_move(
    pile: int,
    idx: int,
    held: dict[int, int],        # BEFORE pick
    new_pile: int,
    new_held: dict[int, int],    # AFTER pick
    new_size: int,
    matched: Optional[int],
) -> tuple[bool, float, dict]:
    ix = _level_idx  # type: ignore[union-attr]

    if new_size >= 7:
        return False, 0.0, {}

    remaining = _get_pile_type_counts(new_pile)
    analysis = _analyze_held(new_held, remaining)

    if analysis["dead_pair_types"]:
        return False, 0.0, analysis

    if _violates_endgame(new_pile, new_held, new_size) and matched is None:
        return False, 0.0, analysis

    # compute helper values
    avail_after = _get_available(new_pile)
    avail_finish_types: set[int] = set()
    for i in ix.iter_bits(avail_after):
        if _will_complete(ix.btype[i], new_held):
            avail_finish_types.add(ix.btype[i])
    finish_next = bool(avail_finish_types)

    btype = ix.btype[idx]
    block_count_after = new_held.get(btype, 0)

    if matched is None and new_size >= 5 and not finish_next:
        # نتحقق: هل يوجد مخرج خلال 3 خطوات؟ إذا نعم نسمح بالحركة
        if not _finish_in_three(new_pile, new_held, new_size):
            return False, 0.0, analysis

    # حالة خاصة: إضافة نوع جديد عند وجود 3+ أنواع غير مكتملة أصلاً
    # نرفض إذا لم يكن هناك مخرج خلال 3 خطوات بعد الماتش
    if (matched is None and new_size >= 4 and not finish_next
            and btype not in held
            and sum(1 for c in held.values() if 0 < c < 3) >= 3):
        if not _finish_in_three(new_pile, new_held, new_size):
            return False, 0.0, analysis

    if matched is None and block_count_after == 2 and btype not in avail_finish_types:
        # نتحقق: هل يوجد مخرج خلال 3 خطوات؟ إذا نعم نسمح بالحركة
        if not _finish_in_three(new_pile, new_held, new_size):
            return False, 0.0, analysis

    # ---- penalties / bonuses ----
    penalty = 0.0

    if analysis["dead_single_types"]:
        penalty -= 1800.0 * len(analysis["dead_single_types"])
    if analysis["open_incomplete_types"] >= 3:
        penalty -= 2200.0 * (analysis["open_incomplete_types"] - 2)
    if analysis["pair_count"] >= 2:
        penalty -= 1400.0 * (analysis["pair_count"] - 1)
    if analysis["single_count"] >= 2:
        penalty -= 550.0 * (analysis["single_count"] - 1)

    new_type = btype not in held
    current_open = sum(1 for c in held.values() if 0 < c < 3)
    open_after = sum(1 for c in new_held.values() if 0 < c < 3)
    unlocks = _get_unlocks(pile, idx)
    strong = ix.reveals_strong_target(pile, idx, held)

    if matched is None and new_type:
        penalty -= max(0, open_after - 2) * 1800.0

    # عقوبة إضافية: إضافة نوع رابع/خامس جديد لليد المتنوعة أصلاً
    # (النوع الجديد ليس مرئياً بشكل كافٍ للإكمال قريباً)
    if matched is None and new_type and current_open >= 3:
        # كم مرة يظهر هذا النوع في المتاح بعد الأخذ؟
        avail_of_new_type = _popcount(_get_available(new_pile) & ix.type_mask.get(btype, 0))
        if avail_of_new_type < 2:  # النوع الجديد غير متاح للإكمال بسرعة
            extra_diversity_pen = (current_open - 2) * 4500.0
            penalty -= extra_diversity_pen

    if matched is None and new_type and current_open >= 2 and new_size >= 5 and unlocks < 2 and not strong:
        penalty -= 3500.0

    if new_size >= 6 and matched is None and new_type and not strong:
        penalty -= 5200.0

    if (
        new_size >= 6
        and new_type
        and matched is None
        and open_after >= 4
        and not strong
        and unlocks == 0
    ):
        return False, 0.0, analysis

    if new_type and matched is None:
        avail_new = _popcount(avail_after & ix.type_mask.get(btype, 0))
        penalty -= 1300.0
        penalty -= current_open * 900.0
        if new_size >= 4:
            penalty -= 2000.0
        if avail_new < 2 and not strong:
            penalty -= 1800.0

    if new_size >= 5 and matched is None:
        penalty -= 900.0 * (new_size - 4)

    if unlocks >= 3:
        penalty += min(unlocks, 5) * 140.0

    if strong:
        penalty += 2400.0

    if matched is None and new_size == 6 and finish_next:
        penalty -= 900.0
    if matched is None and new_size == 5 and finish_next:
        penalty -= 600.0

    return True, penalty, analysis


# ---------------------------------------------------------------------------
#  Lookahead
# ---------------------------------------------------------------------------

def _lookahead(
    pile: int,
    held: dict[int, int],
    held_size: int,
    depth: int,
    beam_width: int,
) -> float:
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    if not avail:
        return _score_state(pile, held, held_size)

    avail_tc = _get_avail_type_counts(pile)

    # priority sort candidates
    candidates: list[tuple[tuple[int, int, int, int], int]] = []
    for i in ix.iter_bits(avail):
        in_hand = held.get(ix.btype[i], 0)
        prio = (
            -int(in_hand >= 2),
            -int(in_hand == 1),
            -_get_unlocks(pile, i),
            -ix.layer[i],
        )
        candidates.append((prio, i))

    # adaptive beam narrowing at depth
    eff = beam_width
    if depth >= 5:
        eff = min(beam_width, 8)
    elif depth >= 4:
        eff = min(beam_width, 12)
    elif depth >= 3:
        eff = min(beam_width, 16)

    candidates.sort(key=lambda x: x[0])
    trimmed = [c[1] for c in candidates[:eff]]
    if not trimmed:
        return _score_state(pile, held, held_size)

    best = -float("inf")

    for i in trimmed:
        new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
        ok, penalty, _ = _assess_post_move(pile, i, held, new_pile, new_held, new_size, matched)
        if not ok:
            continue

        s = _score_state(new_pile, new_held, new_size) + penalty

        in_hand = held.get(ix.btype[i], 0)

        if in_hand == 1:
            avail_after = _get_available(new_pile)
            third_avail = _popcount(avail_after & ix.type_mask.get(ix.btype[i], 0))
            if third_avail:
                s += 1800
            elif _popcount(new_pile & ix.type_mask.get(ix.btype[i], 0)):
                s += 200
            else:
                s -= 2000
        elif in_hand == 0:
            visible = avail_tc.get(ix.btype[i], 0)
            if visible >= 3:
                s += 2000
            elif visible == 2:
                s += 500

        if matched is not None:
            s += 4000

        s += ix.layer[i] * 90
        s += _get_unlocks(pile, i) * 70
        s += _get_depth_below(pile, i) * 50

        if depth > 1:
            s += _lookahead(new_pile, new_held, new_size, depth - 1, beam_width) * 0.5

        best = max(best, s)

    if best == -float("inf"):
        return _score_state(pile, held, held_size) - 2000

    return best


# ---------------------------------------------------------------------------
#  Beam search — main entry point
# ---------------------------------------------------------------------------

def _beam_search(
    pile: int,
    held: dict[int, int],
    held_size: int,
    depth: Optional[int] = None,
    beam_width: int = BEAM_WIDTH,
) -> tuple[Optional[int], str]:
    """Return (block_index | None, reason_string)."""
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    if not avail:
        return None, "no_available_on_board"

    avail_tc = _get_avail_type_counts(pile)
    pile_size = _popcount(pile)

    if depth is None:
        depth = _adaptive_depth(pile_size, held_size)

    # ---- Phase 1: immediate match completions ---
    immediate: list[tuple[float, int]] = []
    for i in ix.iter_bits(avail):
        if not _will_complete(ix.btype[i], held):
            continue

        new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
        if matched is None:
            continue

        remaining = _get_pile_type_counts(new_pile)
        analysis = _analyze_held(new_held, remaining)
        if new_size >= 7:
            continue
        if analysis["dead_pair_types"]:
            continue
        if _violates_endgame(new_pile, new_held, new_size):
            continue

        s = _score_state(new_pile, new_held, new_size)
        s += 9000
        s += _get_unlocks(pile, i) * 120
        s += ix.layer[i] * 120
        s += _get_depth_below(pile, i) * 70
        immediate.append((s, i))

    if immediate:
        immediate.sort(key=lambda x: x[0], reverse=True)
        return immediate[0][1], "ok"

    # ---- Phase 2: general moves ---
    scored: list[tuple[float, int]]       = []
    risky_fallback: list[tuple[float, int]] = []

    for i in ix.iter_bits(avail):
        new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
        ok, penalty, analysis = _assess_post_move(pile, i, held, new_pile, new_held, new_size, matched)
        if not ok:
            continue

        s = _score_state(new_pile, new_held, new_size) + penalty

        btype_i = ix.btype[i]
        in_hand = held.get(btype_i, 0)
        visible = avail_tc.get(btype_i, 0)

        if in_hand == 0 and visible >= 3:
            s += 5200
        elif in_hand == 0 and visible == 2:
            s += 1200

        if matched is not None:
            s += 7000

        if in_hand == 1:
            avail_after = _get_available(new_pile)
            third = _popcount(avail_after & ix.type_mask.get(btype_i, 0))
            if third:
                s += 3200
            elif _popcount(new_pile & ix.type_mask.get(btype_i, 0)):
                s += 400
            else:
                s -= 3500

        s += ix.layer[i] * 160
        s += _get_unlocks(pile, i) * 130
        s += _get_depth_below(pile, i) * 90

        if depth > 1:
            s += _lookahead(new_pile, new_held, new_size, depth - 1, beam_width) * 0.55

        if analysis.get("dead_single_types"):
            risky_fallback.append((s, i))
        else:
            scored.append((s, i))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1], "ok"

    if risky_fallback:
        risky_fallback.sort(key=lambda x: x[0], reverse=True)
        logger.warning("[BOT] كل الخيارات فيها dead single — fallback")
        return risky_fallback[0][1], "ok"

    # ---- Phase 3: Emergency fallback — FIX-3 ذكي ومتدرج ----
    # يُفعَّل فقط عندما ترفض كل الفلاتر الاعتيادية جميع الحركات
    # لكن لا تزال توجد بلوكات متاحة على اللوح.
    # الأولوية: ماتش فوري → مخرج في الخطوة التالية → أعلى unlock → أقل ضرر
    tier1: list[tuple[float, int]] = []   # ماتش فوري
    tier2: list[tuple[float, int]] = []   # finish_next = True بعد الحركة
    tier3: list[tuple[float, int]] = []   # held يبقى <= 4 وعالي الـ unlock
    tier4: list[tuple[float, int]] = []   # أي حركة لا تقتل فوراً

    for i in ix.iter_bits(avail):
        new_pile_e, new_held_e, new_size_e, matched_e = _simulate_pick(pile, held, held_size, i)

        # استبعاد صارم: موت مباشر أو خسارة مؤكدة
        if new_size_e >= 7:
            continue
        remaining_e = _get_pile_type_counts(new_pile_e)
        analysis_e = _analyze_held(new_held_e, remaining_e)
        if analysis_e["dead_pair_types"]:
            continue

        unlocks_e  = _get_unlocks(pile, i)
        layer_e    = ix.layer[i]
        depth_e    = _get_depth_below(pile, i)
        base_score = unlocks_e * 200 + layer_e * 150 + depth_e * 100

        if matched_e is not None:
            # Tier 1: ماتش فوري — دائماً آمن
            tier1.append((base_score + _score_state(new_pile_e, new_held_e, new_size_e), i))
            continue

        # هل يوجد مخرج بعد هذه الحركة؟
        avail_after_e = _get_available(new_pile_e)
        finish_next_e = any(
            _will_complete(ix.btype[j], new_held_e)
            for j in ix.iter_bits(avail_after_e)
        )

        if finish_next_e:
            # Tier 2: يمكننا إكمال ثلاثية في الخطوة القادمة
            tier2.append((base_score, i))
            continue

        if new_size_e <= 4:
            # Tier 3: اليد آمنة (<=4) — لا خطر وشيك
            tier3.append((base_score, i))
            continue

        # Tier 4: آخر ملجأ فقط إذا كان النوع قابلاً للإكمال (≥3 مجموعاً)
        # نرفض أي نوع مستحيل الإكمال حتى في أسوأ الأحوال
        btype_e       = ix.btype[i]
        in_hand_e     = held.get(btype_e, 0)
        board_left_e  = _popcount(new_pile_e & ix.type_mask.get(btype_e, 0))
        need_e        = 3 - (in_hand_e + 1)          # كم باقي للماتش بعد الأخذ

        if board_left_e < need_e:
            continue  # النوع لن يكتمل أبداً — تجاهل حتى في الطوارئ

        # إذا كانت اليد ≥5: نقبل فقط إذا في يدنا مسبقاً زوج أو يوجد ≥2 منه على اللوح
        if new_size_e >= 5:
            if in_hand_e == 0 and board_left_e < 2:
                continue  # نوع جديد ولا يكفي للإكمال
            if in_hand_e == 1 and board_left_e < 1:
                continue  # زوج ولا ثالث موجود

        # T4a: يبقى held ≤ 5 (خطر محدود)
        # T4b: يصل held = 6 (خطر عالي — آخر ملجأ)
        # نفصلهم لنفضل T4a دائماً على T4b
        types_in_hand    = sum(1 for c in held.values() if c > 0)
        completability   = min(board_left_e, 4) * 400
        # هل توجد طريقة خروج خلال 3 خطوات من هذه الحركة؟
        escape_bonus     = 4000 if _finish_in_three(new_pile_e, new_held_e, new_size_e) else 0
        size_pen         = (new_size_e - 4) * 2500

        # --- تقييم نوع الحركة ---
        if in_hand_e == 0:
            # نوع جديد: تنوع إضافي يجعل الوضع أصعب
            # العقوبة تتزايد بشدة مع كثرة الأنواع الموجودة
            diversity_pen = max(0, types_in_hand - 2) * 6000
            pair_bonus    = 0
        elif in_hand_e == 1:
            # نكوّن زوجاً: مفضّل دائماً على إضافة نوع جديد
            diversity_pen = 0
            avail_after   = _get_available(new_pile_e)
            third_visible = _popcount(avail_after & ix.type_mask.get(btype_e, 0))
            pair_bonus    = 3000 if third_visible > 0 else 0  # الثالث مرئي = ممتاز
        else:
            # لدينا زوج بالفعل → ثلاثية فورية (يجب أن تُلتقط في T1 قبل هذا)
            diversity_pen = 0
            pair_bonus    = 5000

        score_e = base_score + completability + escape_bonus + pair_bonus - size_pen - diversity_pen

        if new_size_e <= 5:
            tier4.append((score_e + 10000, i))   # T4a: مفضّل بشدة على T4b
        else:
            tier4.append((score_e, i))            # T4b: فقط إذا لا يوجد T4a

    for tier_name, tier in [("T1-ماتش", tier1), ("T2-مخرج_قادم", tier2),
                              ("T3-يد_آمنة", tier3), ("T4-أخير", tier4)]:
        if tier:
            tier.sort(key=lambda x: x[0], reverse=True)
            ix = _level_idx  # type: ignore[union-attr]
            chosen_type  = ix.btype[tier[0][1]]
            in_hand_cnt  = held.get(chosen_type, 0)
            top3_info    = [(ix.btype[b], held.get(ix.btype[b], 0), round(s, 0))
                            for s, b in tier[:3]]
            logger.warning(
                f"[BOT] طارئ [{tier_name}]: اخترنا نوع={chosen_type} "
                f"(في_اليد={in_hand_cnt}) | يد={held_size}/7 | باقي={_popcount(pile)} | "
                f"اليد_كاملة={dict(held)} | أفضل3={top3_info}"
            )
            return tier[0][1], "ok"

    return None, "all_rejected_by_evaluation"


# ---------------------------------------------------------------------------
#  WebSocket frame builder  (unchanged)
# ---------------------------------------------------------------------------

def build_ws_frame(payload: bytes, masked: bool = True) -> bytes:
    length = len(payload)
    b1 = 0x81
    if length <= 125:
        len_bytes = bytes([0x80 | length if masked else length])
    elif length <= 65535:
        len_bytes = bytes([0x80 | 126 if masked else 126]) + struct.pack(">H", length)
    else:
        len_bytes = bytes([0x80 | 127 if masked else 127]) + struct.pack(">Q", length)

    header = bytes([b1]) + len_bytes
    if not masked:
        return header + payload

    mask_key = os.urandom(4)
    masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return header + mask_key + masked_payload


# ---------------------------------------------------------------------------
#  check_match  — used only for actual GameState (block-level), not search
# ---------------------------------------------------------------------------

def check_match(
    hand: list[dict], storage: list[dict],
) -> tuple[list[dict], list[dict], Optional[int]]:
    matched_type: Optional[int] = None
    while True:
        all_held = hand + storage
        groups: dict[int, list[dict]] = defaultdict(list)
        for b in all_held:
            groups[b["type"]].append(b)
        target = next((t for t, blks in groups.items() if len(blks) >= 3), None)
        if target is None:
            return hand, storage, matched_type
        ids = {b["id"] for b in groups[target][:3]}
        hand    = [b for b in hand    if b["id"] not in ids]
        storage = [b for b in storage if b["id"] not in ids]
        matched_type = target


# ---------------------------------------------------------------------------
#  GameState  (actual state with block dicts for packet building)
# ---------------------------------------------------------------------------

class GameState:
    def __init__(
        self,
        level_idx: LevelIndex,
        pile_blocks: list[dict],
        hand_blocks: Optional[list[dict]] = None,
        storage_blocks: Optional[list[dict]] = None,
    ) -> None:
        self.idx = level_idx

        self.pile_mask: int = 0
        for b in pile_blocks:
            i = level_idx.id_to_idx.get(b["id"])
            if i is not None:
                self.pile_mask |= level_idx.bit[i]

        self.hand: list[dict]    = list(hand_blocks or [])
        self.storage: list[dict] = list(storage_blocks or [])

    def hand_size(self) -> int:
        return len(self.hand) + len(self.storage)

    def held_counts(self) -> dict[int, int]:
        counts: dict[int, int] = defaultdict(int)
        for b in self.hand:
            counts[b["type"]] += 1
        for b in self.storage:
            counts[b["type"]] += 1
        return dict(counts)

    def apply_touch(self, block_id: int) -> tuple[bool, Optional[int]]:
        idx_pos = self.idx.id_to_idx.get(block_id)
        if idx_pos is None:
            return False, None
        if not (self.pile_mask & self.idx.bit[idx_pos]):
            return False, None

        block = self.idx.blocks[idx_pos]
        self.pile_mask ^= self.idx.bit[idx_pos]
        self.hand.append(block)
        self.hand, self.storage, matched = check_match(self.hand, self.storage)
        return True, matched

    def is_dead(self) -> bool:
        return self.hand_size() >= 7

    def is_won(self) -> bool:
        return self.pile_mask == 0 and self.hand_size() == 0


# ---------------------------------------------------------------------------
#  Helpers for addon
# ---------------------------------------------------------------------------

def flush_queue(q: asyncio.Queue) -> int:
    count = 0
    while True:
        try:
            q.get_nowait()
            count += 1
        except asyncio.QueueEmpty:
            break
    return count


# ---------------------------------------------------------------------------
#  CamelBotAddon  — mitmproxy addon (interface unchanged)
# ---------------------------------------------------------------------------

class CamelBotAddon:
    def __init__(self) -> None:
        self.gs: Optional[GameState] = None
        self.rid: Optional[int] = None
        self._flow: Optional[http.HTTPFlow] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._lock = threading.Lock()
        self._bot_thread: Optional[threading.Thread] = None
        self._step = 0
        self._bot_running = False
        self._level = 0
        self._halted_rid: Optional[int] = None
        self._packet_id = 7

    def running(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._queue = asyncio.Queue()
        asyncio.ensure_future(self._sender_coro(), loop=self._loop)
        _clear_caches()
        logger.info("[ADDON] جاهز")

    def websocket_start(self, flow: http.HTTPFlow) -> None:
        with self._lock:
            logger.info("[WS] اتصال جديد — reset")
            self._bot_running = False
            self.gs = None
            self.rid = None
            self._halted_rid = None
            self._flow = None
            self._level = 0
            self._step = 0
            _clear_caches()
            if self._queue:
                flushed = flush_queue(self._queue)
                if flushed:
                    logger.info(f"[WS] حُذفت {flushed} حزمة قديمة")

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if not flow.websocket or not flow.websocket.messages:
            return

        msg = flow.websocket.messages[-1]
        raw = msg.content
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return

        if msg.from_client:
            if obj.get("command") == "CMD_HEART_BEAT":
                rid = obj.get("param", {}).get("rid")
                if rid:
                    with self._lock:
                        if rid != self.rid:
                            self.rid = rid
                            self._halted_rid = None
                            self._step = 0
                            _clear_caches()
                            logger.info(f"[RID] جديد: {rid}")
            return

        data = obj.get("data") or obj.get("message")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = None

        if not isinstance(data, dict) or "pile_blocks" not in data:
            return

        pile_blocks = data["pile_blocks"]
        hand_blocks = data.get("hand_blocks", [])
        storage_blks = data.get("storage_blocks", [])

        start_bot = False
        with self._lock:
            self._flow = flow

            incoming_rid = obj.get("rid") or data.get("rid")
            if incoming_rid and incoming_rid != self.rid:
                self.rid = incoming_rid
                self._halted_rid = None
                self._step = 0
                _clear_caches()
                logger.info(f"[RID] جديد من السيرفر: {self.rid}")

            bot_alive = self._bot_thread is not None and self._bot_thread.is_alive()

            if not bot_alive:
                if self.rid is not None and self.rid == self._halted_rid:
                    logger.info(f"[BOT] موقوف على RID الحالي {self.rid} — لن يعاد تشغيله")
                else:
                    self._level += 1
                    self._step = 0
                    # ---- build spatial index for the new level ----
                    _set_level(pile_blocks)
                    self.gs = GameState(_level_idx, pile_blocks, hand_blocks or [], storage_blks or [])  # type: ignore[arg-type]

                    layers = max((b["layer"] for b in pile_blocks), default=0)
                    avail = _get_available(self.gs.pile_mask)
                    logger.info(
                        f"[LEVEL {self._level}] كتل={len(pile_blocks)} | "
                        f"طبقات={layers} | متاحة={_popcount(avail)} | RID={self.rid}"
                    )
                    start_bot = True
            else:
                # reuse existing LevelIndex, just rebuild GameState with updated masks
                if _level_idx is not None:
                    self.gs = GameState(_level_idx, pile_blocks, hand_blocks or [], storage_blks or [])
                logger.info(
                    f"[UPDATE] كتل={_popcount(self.gs.pile_mask) if self.gs else '?'} "
                    f"| يد={self.gs.hand_size() if self.gs else '?'}"
                )

        if start_bot:
            self._start_bot()

    def _start_bot(self) -> None:
        if self._bot_thread and self._bot_thread.is_alive():
            return
        self._bot_running = True
        self._bot_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._bot_thread.start()

    async def _sender_coro(self) -> None:
        logger.info("[SENDER] بدأ")
        while True:
            if self._queue is None:
                await asyncio.sleep(0.05)
                continue

            item = await self._queue.get()
            if item is _STOP:
                continue

            flow, packet_bytes, is_first, rid = item
            frame = build_ws_frame(packet_bytes, masked=True)
            sent = False

            try:
                transport = getattr(flow.server_conn, "transport", None)
                if transport:
                    transport.write(frame)
                    sent = True
            except Exception as e:
                logger.debug(f"[SENDER] م1: {e}")

            if not sent:
                try:
                    transport = getattr(flow.client_conn, "transport", None)
                    if transport:
                        transport.write(frame)
                        sent = True
                except Exception as e:
                    logger.debug(f"[SENDER] م2: {e}")

            if not sent:
                try:
                    ctx.master.commands.call("inject.websocket", flow, False, packet_bytes, False)
                    sent = True
                except Exception as e:
                    logger.debug(f"[SENDER] م3: {e}")

            if not sent:
                logger.error("[SENDER] ✗ فشلت جميع المحاولات")

    def _play_loop(self) -> None:
        for _ in range(60):
            if self.rid is not None and self._loop is not None:
                break
            time.sleep(0.5)
        else:
            logger.error("[BOT] انتهت مهلة انتظار RID")
            self._bot_running = False
            return

        logger.info("=" * 50)
        logger.info(f"[BOT] بدء المستوى {self._level} | RID={self.rid}")
        logger.info("=" * 50)

        while self._bot_running:
            with self._lock:
                gs_ready = self.gs is not None and self._flow is not None
            if not gs_ready:
                time.sleep(0.1)
                continue

            with self._lock:
                if self.gs is None or self._flow is None:
                    continue
                if self.gs.is_won() or self.gs.is_dead():
                    break
                pile = self.gs.pile_mask
                held = self.gs.held_counts()
                held_size = self.gs.hand_size()
                local_idx = self.gs.idx

            t0 = time.time()
            block_idx, fail_reason = _beam_search(pile, held, held_size)
            dt = time.time() - t0

            if block_idx is None:
                with self._lock:
                    self._halted_rid = self.rid
                    self._bot_running = False
                if fail_reason == "no_available_on_board":
                    logger.warning(f"[BOT] لا توجد نقلة على اللوح! إيقاف نهائي لهذا RID={self.rid}")
                elif fail_reason == "all_rejected_by_evaluation":
                    logger.warning(f"[BOT] توجد نقلات لكن كلّها رُفضت بالتقييم! إيقاف نهائي لهذا RID={self.rid}")
                else:
                    logger.warning(f"[BOT] لا توجد خطوة ممكنة! إيقاف نهائي لهذا RID={self.rid}")
                break

            with self._lock:
                if self.gs is None or self._flow is None:
                    continue
                block = local_idx.blocks[block_idx]
                if not (self.gs.pile_mask & local_idx.bit[block_idx]):
                    continue

                ok, matched = self.gs.apply_touch(block["id"])
                if not ok:
                    logger.warning(f"[BOT] فشل تطبيق البلوك {block['id']}")
                    break

                self._step += 1
                packet = self._build_packet(block).encode("utf-8")
                self._packet_id += 2
                flow_ref = self._flow
                step_n = self._step
                rid_snap = self.rid
                hand_sz = self.gs.hand_size()
                pile_left = _popcount(self.gs.pile_mask)

            if self._queue is not None:
                self._queue.put_nowait((flow_ref, packet, step_n == 1, rid_snap))

            match_str = " -> ماتش!" if matched is not None else ""
            logger.info(
                f"خطوة {step_n:3d}: نوع={block['type']:2d} | طبقة={block['layer']} | "
                f"يد={hand_sz}/7 | باقي={pile_left} | {dt:.3f}s{match_str}"
            )

            time.sleep(SEND_DELAY)

        if self._queue is not None:
            flushed = flush_queue(self._queue)
            if flushed:
                logger.info(f"[BOT] حُذفت {flushed} حزمة متبقية")

        self._bot_running = False

        logger.info("=" * 50)
        if self.gs and self.gs.is_won():
            logger.info(f"فزنا! في {self._step} خطوة")
        elif self.gs and self.gs.is_dead():
            logger.warning("خسرنا! المستودع امتلأ")
        else:
            if self.gs:
                logger.info(f"توقف في خطوة {self._step} | متبقي: {_popcount(self.gs.pile_mask)} كتلة")
        logger.info("=" * 50)

    def _build_packet(self, block: dict) -> str:
        return json.dumps(
            {
                "packet_id": self._packet_id,
                "command": "CMD_TOUCH_BLOCK",
                "param": {
                    "block_id": block["id"],
                    "block_state": block.get("state", 0),
                    "rid": self.rid,
                    "uid": UID,
                    "secret": SECRET,
                },
            }
        )

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        with self._lock:
            if flow is self._flow:
                logger.info("[WS] انغلقت الجلسة")
                self._bot_running = False
                self._flow = None


addons = [CamelBotAddon()]
