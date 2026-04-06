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
import random
import struct
import threading
import time
from collections import defaultdict
from typing import Optional

import subprocess as _subprocess
import sys


class TimerPopup:
    def __init__(self):
        self._proc = None
        self._script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_timer_gui.py")

    def start(self):
        if not os.path.exists(self._script):
            self._create_script()
        try:
            self._proc = _subprocess.Popen(
                [sys.executable, self._script],
                stdin=_subprocess.PIPE,
                creationflags=0x00000008 if sys.platform == "win32" else 0,
            )
        except Exception:
            self._proc = None

    def on_new_rid(self, rid):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write(f"rid:{rid}\n".encode())
                self._proc.stdin.flush()
            except Exception:
                pass

    def _create_script(self):
        code = '''import tkinter as tk
import sys
import time
import threading
import select

root = tk.Tk()
root.title("CamelBot Timer")
root.attributes("-topmost", True)
root.geometry("220x100+50+50")
root.configure(bg="#1a1a2e")
root.resizable(False, False)

rid_label = tk.Label(root, text="Waiting...", font=("Consolas", 10), fg="#e94560", bg="#1a1a2e")
rid_label.pack(pady=(8, 0))

timer_label = tk.Label(root, text="0s", font=("Consolas", 28, "bold"), fg="#0f3460", bg="#1a1a2e")
timer_label.pack(pady=(0, 8))

state = {"start_time": None, "running": False, "delay_id": None}
pending = []

def begin_counting():
    state["start_time"] = time.time()
    state["running"] = True
    state["delay_id"] = None

def update_display():
    if state["running"] and state["start_time"]:
        elapsed = int(time.time() - state["start_time"])
        timer_label.config(text=f"{elapsed}s", fg="#00ff88")
    root.after(200, update_display)

def read_stdin():
    for line in sys.stdin:
        line = line.strip()
        if line.startswith("rid:"):
            pending.append(line[4:])

def check_pending():
    while pending:
        rid = pending.pop(0)
        state["running"] = False
        state["start_time"] = None
        rid_label.config(text=f"RID: {rid}")
        timer_label.config(text="0s", fg="#0f3460")
        if state["delay_id"]:
            root.after_cancel(state["delay_id"])
        state["delay_id"] = root.after(3000, begin_counting)
    root.after(100, check_pending)

t = threading.Thread(target=read_stdin, daemon=True)
t.start()

root.protocol("WM_DELETE_WINDOW", lambda: None)
update_display()
check_pending()
root.mainloop()
'''
        with open(self._script, "w", encoding="utf-8") as f:
            f.write(code)


_timer_popup = TimerPopup()

from mitmproxy import http
from mitmproxy import ctx

try:
    from . import camel_engine_wrapper as _cew
except ImportError:
    try:
        import camel_engine_wrapper as _cew
    except ImportError:
        import importlib.util as _ilu
        _cew_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camel_engine_wrapper.py")
        _cew_spec = _ilu.spec_from_file_location("camel_engine_wrapper", _cew_path)
        _cew = _ilu.module_from_spec(_cew_spec)
        _cew_spec.loader.exec_module(_cew)

_c_engine_ready = False

SECRET       = "11f7257bf19219a61dd1db032b9a7038"
UID          = 398487653
SEND_DELAY   = 0.03
BEAM_WIDTH   = 16
SEARCH_DEPTH = 10
_scoring_noise = 0.0
_fast_mode = False
_tabu_set: set[tuple[int, int]] = set()

_log_dir = os.path.dirname(os.path.abspath(__file__))
_log_file = os.path.join(_log_dir, "camelbot.log")

logger = logging.getLogger("CamelBot")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_sh)
_fh = logging.FileHandler(_log_file, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

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


_board_difficulty = 0
_DBG_DECISION = False
_total_tiles = 225

def _compute_board_difficulty(pile: int) -> int:
    ix = _level_idx
    avail = ix.available_mask(pile)
    avail_types = set()
    for i in ix.iter_bits(avail):
        avail_types.add(ix.btype[i])
    n_types = len(ix.type_mask)
    missing_types = n_types - len(avail_types)
    total_blockers = 0
    count = 0
    for t, tmask in ix.type_mask.items():
        bits = pile & tmask
        while bits:
            bit = bits & -bits
            idx = bit.bit_length() - 1
            cb = ix.covered_by[idx] & pile
            bl = 0
            while cb:
                cb &= cb - 1
                bl += 1
            total_blockers += bl
            count += 1
            bits ^= bit
    avg_blockers = total_blockers / max(count, 1)
    hard_types = 0
    for t, tmask in ix.type_mask.items():
        blockers_list = []
        bits = pile & tmask
        while bits:
            bit = bits & -bits
            idx = bit.bit_length() - 1
            cb = ix.covered_by[idx] & pile
            bl = 0
            while cb:
                cb &= cb - 1
                bl += 1
            blockers_list.append(bl)
            bits ^= bit
        if len(blockers_list) >= 3:
            blockers_list.sort()
            if sum(blockers_list[:3]) >= 6:
                hard_types += 1
    score = missing_types * 3 + hard_types * 2 + int(avg_blockers * 10)
    return score


def _set_level(pile_blocks: list[dict]) -> None:
    global _level_idx, _board_difficulty, _c_engine_ready
    _level_idx = LevelIndex(pile_blocks)
    _clear_caches()
    pile = 0
    for b in pile_blocks:
        i = _level_idx.id_to_idx.get(b["id"])
        if i is not None:
            pile |= _level_idx.bit[i]
    _board_difficulty = _compute_board_difficulty(pile)
    global _cached_pile_blocks
    _cached_pile_blocks = pile_blocks
    try:
        _cew.init_level(pile_blocks)
        _c_engine_ready = True
        logger.info("[C-ENGINE] Level initialized in C engine")
        print(">>> C ENGINE LOADED SUCCESSFULLY <<<")
    except Exception as e:
        _c_engine_ready = False
        logger.warning(f"[C-ENGINE] Failed to init: {e}, falling back to Python")
        print(">>> C ENGINE FAILED - using Python <<<")


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
                    return True
    return False


# ---------------------------------------------------------------------------
#  Via-7/7 rescue path  — 3-step lookahead allowing full-hand match
# ---------------------------------------------------------------------------

def _has_via77_path(pile: int, held: dict[int, int], held_size: int) -> bool:
    """هل يوجد تسلسل 3 خطوات يؤدي لماتش (يسمح بالمرور بـ 7/7 → ماتش فوري)؟"""
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    for j in ix.iter_bits(avail):
        np1, nh1, ns1, m1 = _simulate_pick(pile, held, held_size, j)
        if m1 is not None:
            return True
        if ns1 == 7:
            for k in ix.iter_bits(_get_available(np1)):
                _, _, ns1k, m1k = _simulate_pick(np1, nh1, ns1, k)
                if m1k is not None and ns1k < 7:
                    return True
            continue
        if ns1 > 7:
            continue
        for k in ix.iter_bits(_get_available(np1)):
            np2, nh2, ns2, m2 = _simulate_pick(np1, nh1, ns1, k)
            if m2 is not None:
                return True
            if ns2 == 7:
                for l in ix.iter_bits(_get_available(np2)):
                    _, _, ns2l, m2l = _simulate_pick(np2, nh2, ns2, l)
                    if m2l is not None and ns2l < 7:
                        return True
    return False


# ---------------------------------------------------------------------------
#  Dependency-path helpers  (رسم مسار الكشف)
# ---------------------------------------------------------------------------

def _min_blockers_for_type(pile: int, btype: int) -> int:
    """
    أقل عدد من البلوكات الحاجبة لأي بلوكة من نوع btype لا تزال على اللوح.
    0  = متاح فوراً
    1  = بلوكة واحدة تحجبه
    999 = لا يوجد بلوكات من هذا النوع أصلاً
    """
    ix = _level_idx  # type: ignore[union-attr]
    mask = pile & ix.type_mask.get(btype, 0)
    if not mask:
        return 999
    cb = ix.covered_by
    best = 999
    while mask:
        bit = mask & -mask
        j = bit.bit_length() - 1
        cnt = _popcount(cb[j] & pile)
        if cnt < best:
            best = cnt
            if best == 0:
                break   # متاح فوراً، لا داعي للاستمرار
        mask ^= bit
    return best


def _uncover_score(pile: int, held: dict[int, int], idx: int) -> float:
    """
    مكافأة الحركة *idx* بناءً على مدى اقترابها من كشف بلوكات
    تحتاجها الأنواع الموجودة في اليد.
    تُستخدم في التسجيل العادي وفي Tier-4.
    """
    ix = _level_idx  # type: ignore[union-attr]
    new_pile = pile ^ ix.bit[idx]
    bonus = 0.0

    for t, c in held.items():
        if c == 0 or c >= 3:
            continue
        before = _min_blockers_for_type(pile,     t)
        after  = _min_blockers_for_type(new_pile, t)

        stuck_mult = 1.5 if before >= 3 else 1.0

        if after == 0 and before > 0:
            bonus += 3500.0 * stuck_mult
        elif after < before:
            bonus += 2000.0 * (before - after) * stuck_mult

    return bonus


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
        avail_now = _get_avail_type_counts(pile)
        completable = sum(1 for t, c in held.items() if c == 2 and avail_now.get(t, 0) > 0)
        if completable > 0:
            return -25000.0
        has_pair = any(c == 2 for c in held.values())
        if has_pair:
            return -40000.0
        return -55000.0

    score = 0.0
    remaining = _get_pile_type_counts(pile)
    analysis = _analyze_held(held, remaining)

    pair_rank = 0
    for count in sorted(held.values(), reverse=True):
        if count == 2:
            pair_rank += 1
            if pair_rank == 1:
                score += 2500
            elif pair_rank == 2:
                score += 800
            else:
                score -= 1500
        elif count == 1:
            score -= 600

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

    score -= held_size * 350
    if held_size >= 5:
        score -= 3500
    elif held_size >= 4:
        score -= 1200

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
        return 4
    if pile_size <= 20:
        return 5
    if pile_size <= 40:
        return 4
    if held_size >= 4:
        return 3
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
        if matched is None:
            return False, 0.0, {}

    remaining = _get_pile_type_counts(new_pile)
    analysis = _analyze_held(new_held, remaining)

    if analysis["dead_pair_types"]:
        dead_count = len(analysis["dead_pair_types"])
        if dead_count >= 2:
            return False, 0.0, analysis

    avail_after = _get_available(new_pile)
    avail_finish_types: set[int] = set()
    for i in ix.iter_bits(avail_after):
        if _will_complete(ix.btype[i], new_held):
            avail_finish_types.add(ix.btype[i])
    finish_next = bool(avail_finish_types)

    btype = ix.btype[idx]
    block_count_after = new_held.get(btype, 0)
    unc_rescue = _uncover_score(pile, held, idx)

    stuck_in_hand = sum(1 for t, c in held.items()
                        if 0 < c < 3 and _min_blockers_for_type(pile, t) >= 3)

    is_pair_move = (btype in held and held[btype] == 1)

    pre_size = sum(held.values())
    if matched is None and btype not in held:
        board_of_new = _popcount(new_pile & ix.type_mask.get(btype, 0))
        if board_of_new < 2:
            return False, 0.0, analysis

    # ---- penalties / bonuses ----
    penalty = 0.0

    if analysis["dead_single_types"]:
        penalty -= 1000.0 * len(analysis["dead_single_types"])
    if analysis["open_incomplete_types"] >= 3:
        penalty -= 1200.0 * (analysis["open_incomplete_types"] - 2)
    if analysis["pair_count"] >= 2:
        penalty -= 800.0 * (analysis["pair_count"] - 1)
    if analysis["single_count"] >= 2:
        penalty -= 300.0 * (analysis["single_count"] - 1)

    new_type = btype not in held
    current_open = sum(1 for c in held.values() if 0 < c < 3)
    open_after = sum(1 for c in new_held.values() if 0 < c < 3)
    unlocks = _get_unlocks(pile, idx)
    strong = ix.reveals_strong_target(pile, idx, held)

    if matched is None and new_type:
        penalty -= max(0, open_after - 3) * 800.0

    if matched is None and new_type:
        closest_blocker = _min_blockers_for_type(new_pile, btype)
        if closest_blocker >= 4:
            per_blocker = 2000.0 if closest_blocker >= 5 else 1200.0
            penalty -= closest_blocker * per_blocker

    if matched is None and new_type and current_open >= 4:
        avail_of_new_type = _popcount(_get_available(new_pile) & ix.type_mask.get(btype, 0))
        if avail_of_new_type < 2:
            extra_diversity_pen = (current_open - 3) * 2000.0
            if unc_rescue > 0:
                extra_diversity_pen *= 0.3
            penalty -= extra_diversity_pen

    if matched is None and new_type and current_open >= 3 and new_size >= 6 and unlocks < 2 and not strong:
        pen_val = 1500.0
        if unc_rescue > 0:
            pen_val *= 0.3
        penalty -= pen_val

    if new_size >= 6 and matched is None and new_type and not strong:
        penalty -= 2500.0

    if is_pair_move and matched is None and stuck_in_hand >= 1:
        board_remaining = _popcount(new_pile & ix.type_mask.get(btype, 0))
        if board_remaining >= 1:
            penalty += 6000.0

    if new_type and matched is None:
        avail_new = _popcount(avail_after & ix.type_mask.get(btype, 0))
        penalty -= 600.0
        penalty -= current_open * 400.0
        if new_size >= 5:
            penalty -= 800.0
        if avail_new < 2 and not strong:
            penalty -= 900.0

    if new_size >= 5 and matched is None:
        penalty -= 500.0 * (new_size - 4)

    if unlocks >= 3:
        penalty += min(unlocks, 5) * 140.0

    if strong:
        penalty += 2400.0

    if matched is None and new_size == 6 and finish_next:
        penalty -= 900.0
    if matched is None and new_size == 5 and finish_next:
        penalty -= 600.0

    pair_types_in_hand = [t for t, c in new_held.items() if c == 2]
    if pair_types_in_hand and matched is None:
        reveals_pair_target = False
        uncovered_by_move = ix.covers[idx] & new_pile
        bits_tmp = uncovered_by_move
        while bits_tmp:
            bit_t = bits_tmp & -bits_tmp
            child_idx = bit_t.bit_length() - 1
            if ix.covered_by[child_idx] & new_pile == 0:
                child_type = ix.btype[child_idx]
                if child_type in pair_types_in_hand:
                    reveals_pair_target = True
                    break
            bits_tmp ^= bit_t
        no_pair_avail = True
        for pt in pair_types_in_hand:
            if _popcount(avail_after & ix.type_mask.get(pt, 0)) > 0:
                no_pair_avail = False
                break
        if reveals_pair_target:
            bonus_reveal = 3000.0
            if no_pair_avail:
                bonus_reveal += 2000.0
            if new_size >= 5:
                bonus_reveal += 2000.0
            penalty += bonus_reveal
        if no_pair_avail and new_size >= 5 and not reveals_pair_target:
            penalty -= 2500.0

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
        s += _uncover_score(pile, held, i) * 0.5

        if depth > 1:
            s += _lookahead(new_pile, new_held, new_size, depth - 1, beam_width) * 0.5

        best = max(best, s)

    if best == -float("inf"):
        return _score_state(pile, held, held_size) - 2000

    return best


# ---------------------------------------------------------------------------
#  Monte Carlo rollout — fast heuristic simulation
# ---------------------------------------------------------------------------

def _mc_rollout(pile: int, held: dict[int, int], held_size: int,
                max_steps: int = 500) -> int:
    ix = _level_idx  # type: ignore[union-attr]
    steps = 0
    _btype = ix.btype
    _bit = ix.bit
    _iter = ix.iter_bits

    while steps < max_steps:
        avail = _get_available(pile)
        if avail == 0:
            break

        while True:
            found_match = False
            for i in _iter(avail):
                if held.get(_btype[i], 0) >= 2:
                    pile ^= _bit[i]
                    bt = _btype[i]
                    held = dict(held)
                    held[bt] = held.get(bt, 0) + 1
                    if held[bt] >= 3:
                        held[bt] -= 3
                        if held[bt] == 0:
                            del held[bt]
                        held_size -= 2
                    else:
                        held_size += 1
                    steps += 1
                    found_match = True
                    avail = _get_available(pile)
                    break
            if not found_match:
                break

        if avail == 0:
            break

        # ---- نظام أولويات ذكي لاختيار المسار الأمثل ----
        # Tier 1: pair → الثالثة متاحة الآن  (ماتش بخطوة واحدة)
        # Tier 2: pair → الثالثة موجودة على اللوح  (ماتش قريب)
        # Tier 3: نوع جديد ← 2+ كتل متاحة الآن  (يمكن تشكيل pair+match سريعاً)
        # Tier 4: نوع جديد ← 1 كتلة متاحة الآن   (أبطأ، مقبول باليد الفارغة)
        # Tier 5: نوع جديد ← الكتل مدفونة          (خطر، تجنب إلا اضطراراً)
        _tmask = ix.type_mask
        tier1: list[int] = []
        tier2: list[int] = []
        tier3: list[int] = []
        tier4: list[int] = []
        tier5: list[int] = []

        for i in _iter(avail):
            bt = _btype[i]
            c = held.get(bt, 0)
            tmask_bt = _tmask.get(bt, 0)

            if c == 1:
                # pair move: نتحقق وجود الثالثة
                board_left = _popcount((pile ^ _bit[i]) & tmask_bt)
                if board_left == 0:
                    continue  # dead single, skip
                # هل الثالثة متاحة الآن؟ نستخدم avail الموجود (بدون استدعاء إضافي)
                third_now = _popcount(avail & tmask_bt) - 1  # -1 لأن i نفسه محسوب
                if third_now > 0:
                    tier1.append(i)
                else:
                    tier2.append(i)
            elif c == 0:
                # نوع جديد: نتحقق وجود كتلتين على الأقل على اللوح
                board_rest = _popcount((pile ^ _bit[i]) & tmask_bt)
                if board_rest < 2:
                    continue  # لا يمكن إكمال الثلاثة
                accessible_rest = _popcount(avail & tmask_bt) - 1  # عدد المتاحة الآن غير i
                if accessible_rest >= 2:
                    tier3.append(i)
                elif accessible_rest >= 1:
                    tier4.append(i)
                else:
                    tier5.append(i)

        existing_pool = tier1 + tier2
        new_pool = tier3 + tier4 + tier5
        if held_size >= 5:
            pool = existing_pool if existing_pool else tier3
        elif held_size >= 4:
            pool = existing_pool if existing_pool else (tier3 + tier4)
        elif held_size >= 2:
            pool = existing_pool if existing_pool else (tier3 + tier4)
        else:
            pool = existing_pool + new_pool if existing_pool else new_pool

        if not pool:
            all_avail_list = list(_iter(avail))
            safe_pool = []
            for i in all_avail_list:
                bt_ch = _btype[i]
                c_ch = held.get(bt_ch, 0)
                pile_after = pile ^ _bit[i]
                board_rest = _popcount(pile_after & _tmask.get(bt_ch, 0))
                if c_ch == 1 and board_rest == 0:
                    continue
                if c_ch == 0 and board_rest < 2:
                    continue
                if c_ch + 1 >= 3:
                    safe_pool.append(i)
                elif held_size + 1 < 7:
                    safe_pool.append(i)
            if not safe_pool:
                break
            pool = safe_pool
        if len(pool) <= 2:
            chosen = pool[0] if pool else None
            if chosen is None:
                break
        else:
            scored_pool = []
            for idx_r in pool:
                sc = 0.0
                bt_r = _btype[idx_r]
                c_r = held.get(bt_r, 0)
                if c_r == 1:
                    sc += 3000
                elif c_r == 2:
                    sc += 8000
                sc += ix.layer[idx_r] * 50
                pile_after_r = pile ^ _bit[idx_r]
                children = ix.covers[idx_r] & pile_after_r
                uc = 0
                bits_r = children
                while bits_r:
                    b_r = bits_r & -bits_r
                    ci_r = b_r.bit_length() - 1
                    if ix.covered_by[ci_r] & pile_after_r == 0:
                        uc += 1
                        child_bt = _btype[ci_r]
                        if held.get(child_bt, 0) > 0:
                            sc += 2000
                    bits_r ^= b_r
                sc += uc * 500
                scored_pool.append((sc, idx_r))
            scored_pool.sort(reverse=True)
            top_n = min(3, len(scored_pool))
            chosen = scored_pool[random.randint(0, top_n - 1)][1]

        pile ^= _bit[chosen]
        bt = _btype[chosen]
        held = dict(held)
        held[bt] = held.get(bt, 0) + 1
        held_size += 1
        steps += 1

        if held_size >= 7:
            break

    return steps


def _apply_pick_raw(pile, held, held_size, idx):
    """Apply a single pick: remove from pile, add to hand, auto-match if triple."""
    ix = _level_idx
    pile ^= ix.bit[idx]
    bt = ix.btype[idx]
    held = dict(held)
    held[bt] = held.get(bt, 0) + 1
    held_size += 1
    if held[bt] >= 3:
        held[bt] -= 3
        if held[bt] == 0:
            del held[bt]
        held_size -= 3
    return pile, held, held_size


def _get_greedy_moves(pile, held, held_size):
    """Get sorted candidate moves using tier logic. Returns list of block indices or empty."""
    ix = _level_idx
    _btype = ix.btype
    _tmask = ix.type_mask

    while True:
        avail = _get_available(pile)
        if avail == 0:
            return [], pile, held, held_size
        found = False
        for i in ix.iter_bits(avail):
            if held.get(_btype[i], 0) >= 2:
                pile, held, held_size = _apply_pick_raw(pile, dict(held), held_size, i)
                found = True
                break
        if not found:
            break

    avail = _get_available(pile)
    if avail == 0:
        return [], pile, held, held_size

    tier1, tier2, tier3, tier4, tier5 = [], [], [], [], []
    for i in ix.iter_bits(avail):
        bt = _btype[i]
        c = held.get(bt, 0)
        tmask_bt = _tmask.get(bt, 0)
        if c == 1:
            if _popcount((pile ^ ix.bit[i]) & tmask_bt) == 0:
                continue
            if _popcount(avail & tmask_bt) - 1 > 0:
                tier1.append(i)
            else:
                tier2.append(i)
        elif c == 0:
            if _popcount((pile ^ ix.bit[i]) & tmask_bt) < 2:
                continue
            ar = _popcount(avail & tmask_bt) - 1
            if ar >= 2:
                tier3.append(i)
            elif ar >= 1:
                tier4.append(i)
            else:
                tier5.append(i)

    if held_size >= 6:
        pool = tier1
    elif held_size >= 5:
        pool = tier1 or tier2
    elif held_size >= 4:
        pool = tier1 or tier2 or tier3
    elif held_size >= 2:
        pp = tier1 + tier2
        pool = pp if pp else (tier3 + tier4)
    else:
        pool = tier1 + tier2 + tier3 + tier4 + tier5

    if not pool and held_size <= 5:
        for i in ix.iter_bits(avail):
            bt_ch = _btype[i]
            c_ch = held.get(bt_ch, 0)
            br = _popcount((pile ^ ix.bit[i]) & _tmask.get(bt_ch, 0))
            if c_ch == 1 and br == 0:
                continue
            if c_ch == 0 and br < 2:
                continue
            if c_ch + 1 >= 3 or held_size + 1 < 7:
                pool.append(i)

    if pool:
        pool.sort(key=lambda i: (ix.layer[i] * 3 + _get_unlocks(pile, i)), reverse=True)
    return pool, pile, held, held_size


def _greedy_forward(pile, held, held_size, max_steps):
    """Run greedy simulation forward, return step count."""
    steps = 0
    while steps < max_steps:
        moves, p2, h2, hs2 = _get_greedy_moves(pile, dict(held), held_size)
        if not moves:
            break
        pile, held, held_size = _apply_pick_raw(p2, h2, hs2, moves[0])
        steps += 1
    return steps


def _hf_score_move(pile, held, held_size, i, ix, strict_pair=False):
    """Score a single move for heuristic forward."""
    bt_i = ix.btype[i]
    np, nh, ns, matched = _simulate_pick(pile, held, held_size, i)
    if ns >= 7 and matched is None:
        return None
    remaining = _get_pile_type_counts(pile)
    c_h = nh.get(bt_i, 0)
    c_r = remaining.get(bt_i, 0)
    if bt_i in nh:
        left_in_pile = c_r - (1 if (pile >> i) & 1 else 0)
    else:
        left_in_pile = c_r
    if c_h == 2 and left_in_pile == 0 and matched is None:
        return None
    s = 0.0
    if matched is not None:
        s += 7000
    in_h = held.get(bt_i, 0)
    if _board_difficulty < 87:
        _sp_thresh = 6
    elif _board_difficulty >= 100:
        _sp_thresh = 5
    elif _board_difficulty >= 90:
        _sp_thresh = 3
    else:
        _sp_thresh = 5
    if in_h == 1:
        if strict_pair and ns >= _sp_thresh:
            avail_np = ix.available_mask(np)
            if _popcount(avail_np & ix.type_mask.get(bt_i, 0)):
                s += 5000
            else:
                mb_hf = _min_blockers_for_type(np, bt_i)
                s += (-3000 if mb_hf >= 3 else 1500)
        else:
            s += 3200
    elif in_h == 0:
        s -= 800
    s += ix.layer[i] * 160
    s += _get_unlocks(pile, i) * 130
    s -= ns * 400
    return s


def _heuristic_forward(pile, held, held_size, max_steps, strict_pair=False):
    """Run forward using fast heuristic scoring. Returns step count."""
    ix = _level_idx
    steps = 0
    while steps < max_steps:
        avail = _get_available(pile)
        if avail == 0:
            break
        best_s = -float("inf")
        best_m = -1
        any_valid = False
        for i in ix.iter_bits(avail):
            s = _hf_score_move(pile, held, held_size, i, ix, strict_pair=strict_pair)
            if s is not None and s > best_s:
                best_s = s
                best_m = i
                any_valid = True
        if not any_valid:
            break
        pile, held, held_size = _apply_pick_raw(pile, dict(held), held_size, best_m)
        steps += 1
    return steps


def _beam_game_rollout(pile: int, held: dict[int, int], held_size: int,
                       n_beams: int = 3, max_steps: int = 60) -> int:
    ix = _level_idx
    beams = [(pile, dict(held), held_size)]
    steps = 0

    while steps < max_steps and beams:
        next_beams = []

        for p, h, hs in beams:
            avail = _get_available(p)
            if avail == 0:
                continue

            imm = None
            for i in ix.iter_bits(avail):
                if _will_complete(ix.btype[i], h):
                    np, nh, ns, matched = _simulate_pick(p, h, hs, i)
                    if matched is not None and ns < 7:
                        imm = (np, dict(nh), ns)
                        break

            if imm:
                next_beams.append(imm)
                continue

            scored = []
            for i in ix.iter_bits(avail):
                s = _hf_score_move(p, h, hs, i, ix)
                if s is not None:
                    scored.append((s, i))

            if not scored:
                continue

            scored.sort(key=lambda x: x[0], reverse=True)
            for _, m in scored[:2]:
                np, nh, ns = _apply_pick_raw(p, dict(h), hs, m)
                next_beams.append((np, nh, ns))

        if not next_beams:
            break

        if len(next_beams) > n_beams:
            def _beam_key(b):
                p, h, hs = b
                ps = _popcount(p)
                remaining = _get_pile_type_counts(p)
                dead = 0
                for t, c in h.items():
                    if c == 2 and remaining.get(t, 0) == 0:
                        dead += 1
                stuck = sum(1 for t, c in h.items()
                            if 0 < c < 3 and _min_blockers_for_type(p, t) >= 3)
                pairs = sum(1 for c in h.values() if c == 2)
                return (dead * 50 + ps * 10 + hs * 3 + stuck * 8 - pairs * 4,)
            next_beams.sort(key=_beam_key)
            next_beams = next_beams[:n_beams]

        beams = next_beams
        steps += 1

    return steps


def _bt_rollout(pile: int, held: dict[int, int], held_size: int,
                max_steps: int = 80, bt_budget: int = 6, max_alts: int = 3) -> int:
    """Greedy forward simulation with backtracking. Returns max steps achievable."""
    path = []
    p, h, hs = pile, dict(held), held_size

    while len(path) < max_steps:
        moves, p2, h2, hs2 = _get_greedy_moves(p, dict(h), hs)
        if not moves:
            break
        path.append((p2, dict(h2), hs2, moves))
        p, h, hs = _apply_pick_raw(p2, h2, hs2, moves[0])

    greedy_len = len(path)
    best = greedy_len

    for bt_depth in range(1, min(bt_budget + 1, greedy_len + 1)):
        bt_idx = greedy_len - bt_depth
        bp, bh, bhs, bmoves = path[bt_idx]

        for alt_i in range(1, min(max_alts + 1, len(bmoves))):
            alt_m = bmoves[alt_i]
            ap, ah, ahs = _apply_pick_raw(bp, dict(bh), bhs, alt_m)
            sub = _greedy_forward(ap, ah, ahs, max_steps - bt_idx - 1)
            total = bt_idx + 1 + sub
            if total > best:
                best = total

    return best


def _heuristic_rank(pile: int, held: dict[int, int], held_size: int,
                    i: int) -> float:
    ix = _level_idx  # type: ignore[union-attr]
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return -1e9

    remaining = _get_pile_type_counts(new_pile)
    analysis = _analyze_held(new_held, remaining)
    if analysis["dead_pair_types"]:
        dead_count = len(analysis["dead_pair_types"])
        if dead_count >= 2:
            return -1e9

    btype_i = ix.btype[i]
    in_hand = held.get(btype_i, 0)
    avail_tc = _get_avail_type_counts(pile)

    s = 0.0
    if matched is not None:
        s += 7000

    if in_hand == 0:
        visible = avail_tc.get(btype_i, 0)
        if visible >= 3:
            s += 5000
        elif visible == 2:
            s += 1000
        mb = _min_blockers_for_type(new_pile, btype_i)
        if mb >= 4:
            s -= 4000
        elif mb >= 3:
            s -= 2000
    elif in_hand == 1:
        avail_after = _get_available(new_pile)
        third = _popcount(avail_after & ix.type_mask.get(btype_i, 0))
        if third:
            s += 6000
        else:
            board_left = _popcount(new_pile & ix.type_mask.get(btype_i, 0))
            if board_left:
                s += 1500
            else:
                s -= 3000

    s += ix.layer[i] * 120
    s += _get_unlocks(pile, i) * 100
    s += _get_depth_below(pile, i) * 60

    if in_hand >= 1:
        s += 2000
    if in_hand == 0 and held_size >= 4:
        s -= 3000
    if in_hand == 0 and held_size >= 5:
        s -= 5000

    if new_size >= 6:
        s -= 3000
    elif new_size >= 5:
        s -= 1000

    if analysis.get("dead_pair_types"):
        s -= 8000

    return s


def _mcts_select(pile: int, held: dict[int, int], held_size: int,
                 n_sims: int = 800) -> tuple[Optional[int], str]:
    ix = _level_idx  # type: ignore[union-attr]
    avail = _get_available(pile)
    if avail == 0:
        return None, "no_available_on_board"

    scored: list[tuple[float, int]] = []
    for i in ix.iter_bits(avail):
        h = _heuristic_rank(pile, held, held_size, i)
        if h > -1e8:
            scored.append((h, i))

    if not scored:
        return None, "all_rejected_by_evaluation"

    scored.sort(key=lambda x: x[0], reverse=True)
    hard_board = _board_difficulty >= 90
    mcts_top = min(10 if hard_board else 6, len(scored))
    candidates = [s[1] for s in scored[:mcts_top]]

    sims_per = max(30, n_sims // len(candidates))

    best_avg = -1.0
    best_move = None

    for move in candidates:
        new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, move)
        total = 0
        for _ in range(sims_per):
            total += 1 + _mc_rollout(new_pile, dict(new_held), new_size)
        avg = total / sims_per
        if avg > best_avg:
            best_avg = avg
            best_move = move

    if best_move is not None:
        return best_move, "ok"
    return None, "all_rejected_by_evaluation"


# ---------------------------------------------------------------------------
#  Beam search — main entry point (heuristic + MCTS tiebreaker)
# ---------------------------------------------------------------------------

def _pair_completion_plan(
    pile: int,
    held: dict[int, int],
    held_size: int,
    avail: int,
) -> Optional[int]:
    """Find a move that directly progresses toward completing a pair in hand.
    Returns block index or None.
    
    Strategy:
    1. If a pair type has third available -> pick the second (or third) tile 
    2. If a pair type has third 1 blocker away -> pick the blocker
    3. If a single type has two more available -> pick one
    """
    ix = _level_idx  # type: ignore[union-attr]
    
    best_move = None
    best_priority = -1
    
    for t, c in held.items():
        if c != 1 and c != 2:
            continue
        
        tmask = ix.type_mask.get(t, 0)
        avail_of_t = avail & tmask & pile
        avail_count = _popcount(avail_of_t)
        
        if c == 1 and avail_count >= 1:
            for i in ix.iter_bits(avail_of_t):
                new_pile_p = pile ^ ix.bit[i]
                avail_after_p = _get_available(new_pile_p)
                third_count = _popcount(avail_after_p & tmask & new_pile_p)
                if third_count > 0:
                    priority = 100 + ix.layer[i]
                    if priority > best_priority:
                        new_pile_t, new_held_t, new_size_t, matched_t = _simulate_pick(pile, held, held_size, i)
                        if new_size_t < 7 or matched_t is not None:
                            remaining_t = _get_pile_type_counts(new_pile_t)
                            analysis_t = _analyze_held(new_held_t, remaining_t)
                            if not analysis_t["dead_pair_types"]:
                                best_priority = priority
                                best_move = i
        
        if c == 2 and avail_count >= 1:
            for i in ix.iter_bits(avail_of_t):
                priority = 200 + ix.layer[i]
                if priority > best_priority:
                    best_priority = priority
                    best_move = i
        
        if c == 1 and avail_count == 0:
            board_of_t = pile & tmask
            if not board_of_t:
                continue
            for tile_j in ix.iter_bits(board_of_t):
                blockers = ix.covered_by[tile_j] & pile
                if _popcount(blockers) == 1:
                    blocker_idx = (blockers & -blockers).bit_length() - 1
                    if avail & ix.bit[blocker_idx]:
                        new_pile_b, new_held_b, new_size_b, matched_b = _simulate_pick(pile, held, held_size, blocker_idx)
                        if new_size_b >= 7 and matched_b is None:
                            continue
                        remaining_b = _get_pile_type_counts(new_pile_b)
                        analysis_b = _analyze_held(new_held_b, remaining_b)
                        if analysis_b["dead_pair_types"]:
                            continue
                        blocker_type = ix.btype[blocker_idx]
                        blocker_in_hand = new_held_b.get(blocker_type, 0)
                        priority = 50 + ix.layer[blocker_idx]
                        if blocker_in_hand == 2:
                            priority += 30
                        if priority > best_priority:
                            best_priority = priority
                            best_move = blocker_idx
        
        if c == 2 and avail_count == 0:
            board_of_t = pile & tmask
            if not board_of_t:
                continue
            for tile_j in ix.iter_bits(board_of_t):
                blockers = ix.covered_by[tile_j] & pile
                if _popcount(blockers) == 1:
                    blocker_idx = (blockers & -blockers).bit_length() - 1
                    if avail & ix.bit[blocker_idx]:
                        new_pile_b, new_held_b, new_size_b, matched_b = _simulate_pick(pile, held, held_size, blocker_idx)
                        if new_size_b >= 7 and matched_b is None:
                            continue
                        remaining_b = _get_pile_type_counts(new_pile_b)
                        analysis_b = _analyze_held(new_held_b, remaining_b)
                        if analysis_b["dead_pair_types"]:
                            continue
                        priority = 80 + ix.layer[blocker_idx]
                        if priority > best_priority:
                            best_priority = priority
                            best_move = blocker_idx
    
    return best_move


def _dig_toward_held(
    pile: int,
    held: dict[int, int],
    held_size: int,
    avail: int,
) -> Optional[int]:
    ix = _level_idx
    best_move = None
    best_score = -1

    for t, c in held.items():
        if c <= 0 or c >= 3:
            continue
        tmask = ix.type_mask.get(t, 0)
        avail_of_t = avail & tmask & pile
        if _popcount(avail_of_t) > 0:
            continue

        board_of_t = pile & tmask
        if not board_of_t:
            continue

        for tile_j in ix.iter_bits(board_of_t):
            blockers = ix.covered_by[tile_j] & pile
            blocker_count = _popcount(blockers)
            if blocker_count == 0 or blocker_count > 3:
                continue

            bits_bl = blockers
            while bits_bl:
                bit_bl = bits_bl & -bits_bl
                bl_idx = bit_bl.bit_length() - 1
                bits_bl ^= bit_bl
                if not (avail & ix.bit[bl_idx]):
                    continue

                np_d, nh_d, ns_d, m_d = _simulate_pick(pile, held, held_size, bl_idx)
                if ns_d >= 7 and m_d is None:
                    continue

                score = 0.0
                bl_type = ix.btype[bl_idx]
                bl_c = held.get(bl_type, 0)
                if bl_c >= 2:
                    score += 15000
                elif bl_c == 1:
                    score += 5000
                else:
                    score -= 1000

                score += (4 - blocker_count) * 3000
                score += ix.layer[bl_idx] * 50
                score += _get_unlocks(pile, bl_idx) * 200

                if c == 2:
                    score += 3000

                if score > best_score:
                    best_score = score
                    best_move = bl_idx

    return best_move


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

    hard_board = _board_difficulty >= 90
    avail_tc = _get_avail_type_counts(pile)
    pile_size = _popcount(pile)

    stuck_types = sum(1 for t, c in held.items()
                      if 0 < c < 3 and _min_blockers_for_type(pile, t) > 0)

    if depth is None:
        depth = 2 if _fast_mode else _adaptive_depth(pile_size, held_size)

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
        s += _get_unlocks(pile, i) * 150
        s += ix.layer[i] * 150
        s += _get_depth_below(pile, i) * 100
        s += _uncover_score(pile, held, i) * 2.0
        avail_after_p1 = _get_available(new_pile)
        atc_p1 = {}
        for j in ix.iter_bits(avail_after_p1):
            t_j = ix.btype[j]
            atc_p1[t_j] = atc_p1.get(t_j, 0) + 1
        ft_p1 = sum(1 for cnt in atc_p1.values() if cnt >= 3)
        s += ft_p1 * 1200
        immediate.append((s, i))

    if immediate:
        immediate.sort(key=lambda x: x[0], reverse=True)
        return immediate[0][1], "ok"

    if held_size >= 3 and not _fast_mode:
        pair_plan = _pair_completion_plan(pile, held, held_size, avail)
        if pair_plan is not None:
            pp_pile, pp_held, pp_size, pp_matched = _simulate_pick(pile, held, held_size, pair_plan)
            if pp_matched is not None or pp_size < 7:
                ok_pp, _, _ = _assess_post_move(pile, pair_plan, held, pp_pile, pp_held, pp_size, pp_matched)
                if ok_pp:
                    return pair_plan, "ok"

    if held_size >= 3 and not _fast_mode:
        dig_move = _dig_toward_held(pile, held, held_size, avail)
        if dig_move is not None:
            dm_pile, dm_held, dm_size, dm_matched = _simulate_pick(pile, held, held_size, dig_move)
            if dm_size < 7 or dm_matched is not None:
                ok_dm, _, _ = _assess_post_move(pile, dig_move, held, dm_pile, dm_held, dm_size, dm_matched)
                if ok_dm:
                    return dig_move, "ok"

    if held_size >= 4 and not _fast_mode:
        mcts_sims = 1200 if held_size <= 4 else (2000 if held_size <= 5 else 3000)
        mcts_result = _mcts_select(pile, held, held_size, n_sims=mcts_sims)
        if mcts_result[0] is not None:
            return mcts_result

    # ---- Phase 2: general moves (heuristic + lookahead) ---
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
            board_left_t = _popcount(new_pile & ix.type_mask.get(btype_i, 0))
            if third:
                pair_bonus = 12000 if stuck_types >= 2 else 6000
                s += pair_bonus
            elif board_left_t:
                mb_for_pair = _min_blockers_for_type(new_pile, btype_i)
                if mb_for_pair >= 3:
                    s -= 3000
                elif mb_for_pair >= 2:
                    s += 1000
                else:
                    s += 4000
            else:
                s -= 2000

        avail_after_p2 = _get_available(new_pile)
        avail_tc_after_p2 = {}
        for j in ix.iter_bits(avail_after_p2):
            t_j = ix.btype[j]
            avail_tc_after_p2[t_j] = avail_tc_after_p2.get(t_j, 0) + 1
        fresh_triples = sum(1 for cnt in avail_tc_after_p2.values() if cnt >= 3)
        s += fresh_triples * 1500
        pairs_avail = sum(1 for cnt in avail_tc_after_p2.values() if cnt == 2)
        s += pairs_avail * 300
        if fresh_triples == 0 and new_size >= 3:
            s -= 3000

        s += ix.layer[i] * 160
        s += _get_unlocks(pile, i) * 200
        s += _get_depth_below(pile, i) * 120
        zombie_types = sum(1 for t, c in held.items()
                          if 0 < c < 3 and _popcount(avail_after_p2 & ix.type_mask.get(t, 0)) == 0
                          and _popcount(new_pile & ix.type_mask.get(t, 0)) > 0)
        unc_mult = 5.0 if zombie_types >= 2 else (3.5 if zombie_types >= 1 else 2.0)
        s += _uncover_score(pile, held, i) * unc_mult

        if depth > 1:
            la_coeff = 0.55
            if (matched is None and btype_i not in held
                    and _min_blockers_for_type(new_pile, btype_i) >= 4):
                la_coeff = 0.15
            s += _lookahead(new_pile, new_held, new_size, depth - 1, beam_width) * la_coeff

        if _scoring_noise > 0:
            s += random.gauss(0, _scoring_noise)

        if _tabu_set and (pile, i) in _tabu_set:
            s -= 100000

        if analysis.get("dead_single_types"):
            risky_fallback.append((s, i))
        else:
            scored.append((s, i))

    pool = scored if scored else risky_fallback

    pair_types_bs = [t for t, c in held.items() if c == 2]
    if pair_types_bs and held_size >= 4:
        no_pair_match_bs = True
        for pt_bs in pair_types_bs:
            if _popcount(avail & ix.type_mask.get(pt_bs, 0)) > 0:
                no_pair_match_bs = False
                break
        if no_pair_match_bs and pool:
            reveal_pool = []
            for s_bs, i_bs in pool:
                uncov_bs = ix.covers[i_bs] & (pile & ~(1 << i_bs))
                bits_bs = uncov_bs
                reveals_bs = False
                while bits_bs:
                    bit_bs = bits_bs & -bits_bs
                    ci_bs = bit_bs.bit_length() - 1
                    p_after = pile & ~(1 << i_bs)
                    if ix.covered_by[ci_bs] & p_after == 0:
                        if ix.btype[ci_bs] in pair_types_bs:
                            reveals_bs = True
                            break
                    bits_bs ^= bit_bs
                if reveals_bs:
                    reveal_pool.append((s_bs + 5000, i_bs))
            if reveal_pool:
                pool = reveal_pool

    current_singles = sum(1 for c in held.values() if c == 1)
    if held_size >= 4:
        existing_scored = [(s, i) for s, i in scored if held.get(ix.btype[i], 0) >= 1]
        existing_risky = [(s, i) for s, i in risky_fallback if held.get(ix.btype[i], 0) >= 1]
        existing_pool = existing_scored if existing_scored else existing_risky
        if existing_pool:
            pool = existing_pool

    if pool:
        pool.sort(key=lambda x: x[0], reverse=True)

        if _fast_mode:
            return pool[0][1], "ok"
        veto_top = min(10 if hard_board else 6, len(pool))
        veto_threshold = 2 if hard_board else 3
        top_items = pool[:veto_top]
        reaches_bt = []
        reaches_hf = []
        for _, m in top_items:
            np, nh, ns, _ = _simulate_pick(pile, held, held_size, m)
            r_bt = 1 + _bt_rollout(np, dict(nh), ns)
            r_hf = 1 + _heuristic_forward(np, dict(nh), ns, 80, strict_pair=True)
            reaches_bt.append((r_bt, m))
            reaches_hf.append((r_hf, m))

        heur_pick = top_items[0][1]

        heur_reach_bt = next(r for r, m in reaches_bt if m == heur_pick)
        best_bt = max(reaches_bt, key=lambda x: x[0])

        heur_reach_hf = next(r for r, m in reaches_hf if m == heur_pick)
        best_hf = max(reaches_hf, key=lambda x: x[0])

        bt_veto = best_bt[0] >= heur_reach_bt + veto_threshold and best_bt[1] != heur_pick
        hf_veto = best_hf[0] >= heur_reach_hf + veto_threshold and best_hf[1] != heur_pick

        if _DBG_DECISION:
            _n_tiles_on = _popcount(pile)
            cleared = _total_tiles - _n_tiles_on - held_size
            print(f"  [P2] step~{cleared} hs={held_size} heur_pick=t{ix.btype[heur_pick]}(tile{heur_pick}) bt_veto={bt_veto} hf_veto={hf_veto}")
            for s_val, m in top_items[:5]:
                r_bt_v = next(r for r, mm in reaches_bt if mm == m)
                r_hf_v = next(r for r, mm in reaches_hf if mm == m)
                ih = held.get(ix.btype[m], 0)
                tag = "MATCH" if ih==2 else ("pair" if ih==1 else "new")
                print(f"    tile{m:3d} t={ix.btype[m]:2d} ({tag}) score={int(s_val):7d} bt={r_bt_v:3d} hf={r_hf_v:3d}")

        if bt_veto:
            if _DBG_DECISION:
                print(f"  -> BT VETO: tile{best_bt[1]} t={ix.btype[best_bt[1]]}")
            return best_bt[1], "ok"
        if hf_veto and not bt_veto:
            hf_candidate = best_hf[1]
            hf_bt_reach = next(r for r, m in reaches_bt if m == hf_candidate)
            if hf_bt_reach >= heur_reach_bt:
                if _DBG_DECISION:
                    print(f"  -> HF VETO: tile{hf_candidate} t={ix.btype[hf_candidate]}")
                return hf_candidate, "ok"

        return heur_pick, "ok"

    if _fast_mode:
        return None, "fast_mode_no_move"
    # ---- Phase 3: MCTS emergency fallback ----
    p3_result = _mcts_select(pile, held, held_size)
    if p3_result[0] is not None:
        return p3_result

    t1_em: list[tuple[float, int]] = []
    t2_em: list[tuple[float, int]] = []
    t3_em: list[tuple[float, int]] = []
    t4_em: list[tuple[float, int]] = []
    t5_em: list[tuple[float, int]] = []
    t6_em: list[tuple[float, int]] = []

    for i in ix.iter_bits(avail):
        new_pile_e, new_held_e, new_size_e, matched_e = _simulate_pick(pile, held, held_size, i)

        btype_e = ix.btype[i]
        is_pair_e = (held.get(btype_e, 0) == 1)
        is_match_e = (held.get(btype_e, 0) == 2)

        if new_size_e >= 7 and matched_e is None:
            continue

        remaining_e = _get_pile_type_counts(new_pile_e)
        analysis_e = _analyze_held(new_held_e, remaining_e)
        if analysis_e["dead_pair_types"]:
            if len(analysis_e["dead_pair_types"]) >= 2:
                continue

        em_score = ix.layer[i] * 100.0 + _get_unlocks(pile, i) * 80.0
        em_score += _uncover_score(pile, held, i) * 0.5

        if matched_e is not None:
            t1_em.append((em_score + 10000, i))
        elif is_pair_e:
            mb_e = _min_blockers_for_type(new_pile_e, btype_e)
            if mb_e <= 1:
                t2_em.append((em_score + 5000, i))
            elif new_size_e <= 4:
                t3_em.append((em_score, i))
            elif new_size_e <= 5:
                t4_em.append((em_score, i))
            else:
                t5_em.append((em_score - mb_e * 300, i))
        elif new_size_e <= 4:
            t3_em.append((em_score, i))
        elif new_size_e <= 5:
            t4_em.append((em_score, i))
        else:
            t5_em.append((em_score, i))

    em_pool = t1_em or t2_em or t3_em or t4_em or t5_em
    if em_pool:
        em_pool.sort(key=lambda x: x[0], reverse=True)
        em_candidates = [m for _, m in em_pool[:8]]
        sims = max(30, 400 // len(em_candidates))
        best_avg = -1.0
        best_move = None
        for m in em_candidates:
            np_e, nh_e, ns_e, _ = _simulate_pick(pile, held, held_size, m)
            total = 0
            for _ in range(sims):
                total += 1 + _mc_rollout(np_e, dict(nh_e), ns_e)
            avg = total / sims
            if avg > best_avg:
                best_avg = avg
                best_move = m
        if best_move is not None:
            return best_move, "ok"

    last_resort: list[tuple[float, int]] = []
    for i in ix.iter_bits(avail):
        new_pile_lr, new_held_lr, new_size_lr, matched_lr = _simulate_pick(pile, held, held_size, i)
        if new_size_lr >= 7 and matched_lr is None:
            continue
        btype_lr = ix.btype[i]
        lr_score = 0.0

        if matched_lr is not None:
            lr_score += 50000
        elif held.get(btype_lr, 0) == 1:
            board_left_lr = _popcount(new_pile_lr & ix.type_mask.get(btype_lr, 0))
            lr_score += 10000 + board_left_lr * 500
            avail_lr = _get_available(new_pile_lr)
            third_avail_lr = _popcount(avail_lr & ix.type_mask.get(btype_lr, 0))
            if third_avail_lr > 0:
                lr_score += 20000
            else:
                mb_lr = _min_blockers_for_type(new_pile_lr, btype_lr)
                lr_score += max(0, 5000 - mb_lr * 1000)
        elif held.get(btype_lr, 0) == 2:
            lr_score += 40000
        else:
            lr_score -= 5000

        lr_score += ix.layer[i] * 50.0
        lr_score += _get_unlocks(pile, i) * 100.0
        last_resort.append((lr_score, i))

    if last_resort:
        last_resort.sort(key=lambda x: x[0], reverse=True)
        lr_candidates = [m for _, m in last_resort[:6]]
        sims_lr = max(20, 200 // len(lr_candidates))
        best_avg_lr = -1.0
        best_move_lr = None
        for m in lr_candidates:
            np_lr, nh_lr, ns_lr, _ = _simulate_pick(pile, held, held_size, m)
            total_lr = 0
            for _ in range(sims_lr):
                total_lr += 1 + _mc_rollout(np_lr, dict(nh_lr), ns_lr)
            avg_lr = total_lr / sims_lr
            if avg_lr > best_avg_lr:
                best_avg_lr = avg_lr
                best_move_lr = m
        if best_move_lr is not None:
            return best_move_lr, "ok"

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
#  DFS-based Solution Pre-Planner
# ---------------------------------------------------------------------------

def _dfs_quick_score(pile, held, held_size, i):
    ix = _level_idx
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched
    remaining = _get_pile_type_counts(new_pile)
    analysis = _analyze_held(new_held, remaining)
    if analysis.get("dead_pair_types"):
        return None, new_pile, new_held, new_size, matched
    
    s = _score_state(new_pile, new_held, new_size)

    avail_before = _get_available(pile)
    avail_tc = {}
    for j in ix.iter_bits(avail_before):
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
            mb = _min_blockers_for_type(new_pile, bt)
            if mb >= 3:
                s -= 5000
            elif mb >= 2:
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


def _plan_score_move(pile, held, held_size, i, ix, variant=0, relaxed=False):
    bt = ix.btype[i]
    ih = held.get(bt, 0)
    new_pile, new_held, new_size, matched = _simulate_pick(pile, held, held_size, i)
    if new_size >= 7 and matched is None:
        return None, new_pile, new_held, new_size, matched

    remaining = _get_pile_type_counts(new_pile)
    analysis = _analyze_held(new_held, remaining)
    dead_pairs = len(analysis.get("dead_pair_types", []))
    if not relaxed:
        if dead_pairs >= 2:
            return None, new_pile, new_held, new_size, matched
        if dead_pairs >= 1 and new_size >= 6:
            return None, new_pile, new_held, new_size, matched

    avail_after = _get_available(new_pile)
    avail_count_after = _popcount(avail_after)

    s = 0.0
    if matched is not None:
        s += 15000
    if dead_pairs:
        s -= 12000

    if ih == 2:
        s += 20000
    elif ih == 1:
        third_avail = _popcount(avail_after & ix.type_mask.get(bt, 0))
        if third_avail > 0:
            s += 8000
        else:
            mb = _min_blockers_for_type(new_pile, bt)
            if mb <= 1:
                s += 3000
            elif mb <= 2:
                s += 500
            else:
                s -= 2000 - mb * 300
    elif ih == 0:
        board_left = _popcount(new_pile & ix.type_mask.get(bt, 0))
        if board_left < 2:
            s -= 15000
        visible = _popcount(avail_after & ix.type_mask.get(bt, 0))
        if visible >= 2:
            s += 6000
        elif visible >= 1:
            s += 2000
        elif board_left >= 2:
            mb = _min_blockers_for_type(new_pile, bt)
            if new_size >= 4:
                s -= 2000 - mb * 400
            else:
                s -= 300

    _unlock_variants = [600, 800, 400, 1000, 200, 900, 1200, 300, 700, 500, 150, 1500, 2000, 0, 0, 0, 1500, 100, 50, 1800]
    _avail_variants  = [400, 600, 300, 200, 100, 500, 800, 150, 700, 350, 50, 250, 0, 2000, 0, 0, 1500, 100, 1200, 200]
    _depth_variants  = [200, 100, 300, 400, 50, 350, 500, 250, 150, 600, 25, 100, 0, 0, 2000, 0, 0, 1000, 800, 50]
    _hand_variants   = [400, 300, 500, 600, 800, 250, 200, 700, 350, 150, 900, 450, 0, 0, 0, 2000, 100, 1000, 100, 1200]
    nv = len(_unlock_variants)
    unlock_w = _unlock_variants[variant % nv]
    avail_w = _avail_variants[variant % nv]
    depth_w = _depth_variants[variant % nv]
    hand_w = _hand_variants[variant % nv]

    s += avail_count_after * avail_w
    s += _get_unlocks(pile, i) * unlock_w
    s += _get_depth_below(pile, i) * depth_w
    s += ix.layer[i] * 100

    zombie_count = 0
    for t, c in new_held.items():
        if 0 < c < 3 and _popcount(avail_after & ix.type_mask.get(t, 0)) == 0:
            if _popcount(new_pile & ix.type_mask.get(t, 0)) > 0:
                zombie_count += 1
    s -= zombie_count * 1500

    s -= new_size * hand_w
    if new_size >= 5:
        s -= 3000
    if new_size >= 6:
        s -= 6000

    open_types = sum(1 for c in new_held.values() if 0 < c < 3)
    if open_types >= 4:
        s -= (open_types - 3) * 1500

    return s, new_pile, new_held, new_size, matched


_cached_pile_blocks = None


def _run_c_engine_in_process(held, held_size, time_limit):
    global _cached_pile_blocks
    if _cached_pile_blocks is None:
        raise RuntimeError("No pile_blocks cached")

    _dir = os.path.dirname(os.path.abspath(__file__))
    solver_script = os.path.join(_dir, "_c_solver.py")

    if not os.path.exists(solver_script):
        with open(solver_script, "w", encoding="utf-8") as f:
            f.write('''import sys, json, os
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
sys.path.insert(0, sys.argv[1])
import camel_engine_wrapper as w
data = json.loads(sys.stdin.read())
w.init_level(data["pile_blocks"])
held = {int(k): v for k, v in data["held"].items()}
path, trials = w.plan(held, data["held_size"], time_limit=data["time_limit"])
print(json.dumps({"path": path, "trials": trials}))
''')

    input_data = json.dumps({
        "pile_blocks": _cached_pile_blocks,
        "held": {str(k): v for k, v in held.items()},
        "held_size": held_size,
        "time_limit": time_limit,
    })

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008

    proc = _subprocess.Popen(
        [sys.executable, solver_script, _dir],
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.PIPE,
        **kwargs,
    )
    stdout, stderr = proc.communicate(input=input_data.encode(), timeout=time_limit + 15)

    if proc.returncode != 0:
        raise RuntimeError(f"C solver failed: {stderr.decode()[:500]}")

    result = json.loads(stdout.decode())
    return result["path"], result["trials"]


def _plan_solution(pile, held, held_size, time_limit=8.0):
    ix = _level_idx
    if ix is None:
        return []

    import time as _time

    if _c_engine_ready:
        try:
            t0 = _time.time()
            c_path, c_trials = _run_c_engine_in_process(held, held_size, time_limit)
            elapsed = _time.time() - t0
            logger.info(f"[C-ENGINE] Best: {len(c_path)} steps in {elapsed:.1f}s ({c_trials} trials)")
            print(f">>> C ENGINE PLAN: {len(c_path)} steps in {elapsed:.1f}s <<<")
            if c_path:
                return c_path
        except Exception as e:
            logger.warning(f"[C-ENGINE] Error: {e}, falling back to Python")
            print(f">>> C ENGINE ERROR: {e} <<<")

    t0 = _time.time()

    best_plan = []
    best_steps = 0

    beam_w = 60
    branch_factor = 10

    def _auto_match(p, h, hs, path, smart=True):
        changed = True
        while changed:
            changed = False
            avail = _get_available(p)
            if not avail:
                break
            if smart:
                matches = []
                for i in ix.iter_bits(avail):
                    if h.get(ix.btype[i], 0) >= 2:
                        unlocks = _get_unlocks(p, i)
                        matches.append((unlocks, i))
                if matches:
                    matches.sort(reverse=True)
                    best_i = matches[0][1]
                    p, h, hs = _apply_pick_raw(p, dict(h), hs, best_i)
                    path.append(best_i)
                    changed = True
            else:
                for i in ix.iter_bits(avail):
                    if h.get(ix.btype[i], 0) >= 2:
                        p, h, hs = _apply_pick_raw(p, dict(h), hs, i)
                        path.append(i)
                        changed = True
                        break
        return p, h, hs, path

    for variant in range(6):
        for use_smart in [True, False]:
            if _time.time() - t0 > time_limit * 0.50:
                break
            initial_pile, initial_held, initial_hs, initial_path = _auto_match(
                pile, dict(held), held_size, [], smart=use_smart)

            beams = [(0.0, initial_pile, initial_held, initial_hs, initial_path)]
            if len(initial_path) > best_steps:
                best_steps = len(initial_path)
                best_plan = initial_path[:]

            for round_num in range(225):
                if _time.time() - t0 > time_limit * 0.50:
                    break
                if not beams:
                    break

                next_beams = []
                for beam_score, p, h, hs, path in beams:
                    avail = _get_available(p)
                    if not avail:
                        if len(path) > best_steps:
                            best_steps = len(path)
                            best_plan = path[:]
                        continue

                    cands = []
                    for i in ix.iter_bits(avail):
                        r = _plan_score_move(p, h, hs, i, ix, variant=variant)
                        if r[0] is None:
                            continue
                        cands.append((r[0], i, r[1], r[2], r[3]))
                    if not cands:
                        if len(path) > best_steps:
                            best_steps = len(path)
                            best_plan = path[:]
                        continue

                    cands.sort(key=lambda x: x[0], reverse=True)
                    for sc, bi, np, nh, ns in cands[:branch_factor]:
                        new_path = path + [bi]
                        np2, nh2, ns2, new_path2 = _auto_match(np, dict(nh), ns, new_path, smart=use_smart)
                        state_score = beam_score + sc + len(new_path2) * 500
                        next_beams.append((state_score, np2, nh2, ns2, new_path2))

                        if len(new_path2) > best_steps:
                            best_steps = len(new_path2)
                            best_plan = new_path2[:]

                if not next_beams:
                    break

                next_beams.sort(key=lambda x: x[0], reverse=True)

                seen = set()
                deduped = []
                for item in next_beams:
                    key = (item[1], _held_key(item[2]))
                    if key not in seen:
                        seen.add(key)
                        deduped.append(item)
                        if len(deduped) >= beam_w:
                            break
                beams = deduped

    noise_levels = [0, 100, 200, 500, 1000, 2000, 3000, 5000, 8000, 12000]
    trial = 0
    noisy_limit = time_limit * 0.80
    for noise in noise_levels:
        for seed in range(300):
            if _time.time() - t0 > noisy_limit:
                break
            trial += 1

            use_smart = seed % 2 == 0
            variant = seed % 20
            use_relaxed = seed % 4 != 0
            random.seed(seed * 100 + noise)
            _score_cache.clear()

            p, h, hs = pile, dict(held), held_size
            path = []

            while len(path) < 225:
                p, h, hs, path = _auto_match(p, dict(h), hs, path, smart=use_smart)
                avail = _get_available(p)
                if not avail:
                    break
                cands = []
                for i in ix.iter_bits(avail):
                    r = _plan_score_move(p, h, hs, i, ix, variant=variant, relaxed=use_relaxed)
                    if r[0] is None:
                        continue
                    sc = r[0]
                    if noise > 0:
                        sc += random.gauss(0, noise)
                    cands.append((sc, i, r[1], r[2], r[3]))
                if not cands:
                    break
                cands.sort(key=lambda x: x[0], reverse=True)
                sc, bi, np, nh, ns = cands[0]
                p, h, hs = np, nh, ns
                path.append(bi)

            if len(path) > best_steps:
                best_steps = len(path)
                best_plan = path[:]

    if best_steps < 150 and best_plan:
        bt_limit = time_limit * 0.88
        for bt_seed in range(50000):
            if _time.time() - t0 > bt_limit:
                break
            random.seed(bt_seed * 31337 + 7)
            _score_cache.clear()
            use_smart = bt_seed % 2 == 0
            variant = bt_seed % 20

            backtrack_point = random.randint(max(0, best_steps - 40), max(0, best_steps - 5))
            prefix = best_plan[:backtrack_point]

            p, h, hs = pile, dict(held), held_size
            ok = True
            for bi in prefix:
                if not (p & ix.bit[bi]):
                    ok = False
                    break
                p, h, hs = _apply_pick_raw(p, dict(h), hs, bi)
            if not ok:
                continue

            path = list(prefix)
            while len(path) < 225:
                p, h, hs, path = _auto_match(p, dict(h), hs, path, smart=use_smart)
                avail = _get_available(p)
                if not avail:
                    break
                cands = []
                for i in ix.iter_bits(avail):
                    r = _plan_score_move(p, h, hs, i, ix, variant=variant, relaxed=False)
                    if r[0] is None:
                        continue
                    sc = r[0]
                    sc += random.gauss(0, 3000)
                    cands.append((sc, i, r[1], r[2], r[3]))
                if not cands:
                    break
                cands.sort(key=lambda x: x[0], reverse=True)
                sc, bi, np, nh, ns = cands[0]
                p, h, hs = np, nh, ns
                path.append(bi)

            if len(path) > best_steps:
                best_steps = len(path)
                best_plan = path[:]

    if best_steps < 150:
        import math as _math
        for retry_seed in range(10000):
            if _time.time() - t0 > time_limit:
                break
            random.seed(retry_seed * 54321 + 99)
            _score_cache.clear()
            use_smart = retry_seed % 3 != 2
            variant = retry_seed % 20
            use_relaxed = retry_seed % 4 != 0

            p, h, hs = pile, dict(held), held_size
            path = []

            while len(path) < 225:
                p, h, hs, path = _auto_match(p, dict(h), hs, path, smart=use_smart)
                avail = _get_available(p)
                if not avail:
                    break
                cands = []
                for i in ix.iter_bits(avail):
                    r = _plan_score_move(p, h, hs, i, ix, variant=variant, relaxed=use_relaxed)
                    if r[0] is None:
                        continue
                    cands.append((r[0], i, r[1], r[2], r[3]))
                if not cands:
                    break
                cands.sort(key=lambda x: x[0], reverse=True)
                temp = max(500.0, 8000.0 * (1.0 - len(path) / 200.0))
                weights = []
                for sc, i, np2, nh2, ns2 in cands:
                    w = _math.exp((sc - cands[0][0]) / temp)
                    weights.append(w)
                total_w = sum(weights)
                r = random.random() * total_w
                cumul = 0
                pick_idx = 0
                for idx_w, w in enumerate(weights):
                    cumul += w
                    if cumul >= r:
                        pick_idx = idx_w
                        break
                sc, bi, np, nh, ns = cands[pick_idx]
                p, h, hs = np, nh, ns
                path.append(bi)

            if len(path) > best_steps:
                best_steps = len(path)
                best_plan = path[:]

    logger.info(f"[PLAN] Best: {best_steps} steps in {_time.time()-t0:.1f}s")

    return best_plan


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
        _timer_popup.start()

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
                            _timer_popup.on_new_rid(rid)
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
                _timer_popup.on_new_rid(self.rid)

            bot_alive = self._bot_thread is not None and self._bot_thread.is_alive()

            if not bot_alive:
                if self.rid is not None and self.rid == self._halted_rid:
                    logger.info(f"[BOT] موقوف على RID الحالي {self.rid} — لن يعاد تشغيله")
                elif len(pile_blocks) < 20:
                    logger.info(f"[SKIP] تجاهل مستوى تعليمي ({len(pile_blocks)} كتلة < 20)")
                else:
                    self._level += 1
                    self._step = 0
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
                if self._bot_running:
                    logger.info(
                        f"[UPDATE] تجاهل (البوت شغال) | سيرفر: كتل={len(pile_blocks)} يد={len(hand_blocks)}"
                    )
                else:
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

        planned_moves = []
        plan_idx = 0

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

            if not planned_moves and self._step == 0:
                logger.info("[BOT] Pre-planning solution...")
                planned_moves = _plan_solution(pile, held, held_size, time_limit=8.0)
                plan_idx = 0
                logger.info(f"[BOT] Plan: {len(planned_moves)} moves")

            block_idx = None
            fail_reason = ""

            if plan_idx < len(planned_moves):
                candidate = planned_moves[plan_idx]
                ix = _level_idx
                avail_mask = _get_available(pile) if ix else 0
                if ix and (avail_mask & ix.bit[candidate]):
                    block_idx = candidate
                    plan_idx += 1
                else:
                    logger.info(f"[BOT] Plan deviated at step {self._step}, re-planning...")
                    planned_moves = _plan_solution(pile, held, held_size, time_limit=5.0)
                    plan_idx = 0
                    if planned_moves:
                        replan_candidate = planned_moves[0]
                        replan_avail = _get_available(pile) if ix else 0
                        if ix and (replan_avail & ix.bit[replan_candidate]):
                            block_idx = replan_candidate
                            plan_idx = 1
                        else:
                            planned_moves = []
                            plan_idx = 0

            if block_idx is None:
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
                logger.info(f"[MOVE] #{step_n} tile={block['id']} type={block['type']} hand={hand_sz} pile={pile_left}")

            time.sleep(SEND_DELAY)

        if self._queue is not None:
            flushed = flush_queue(self._queue)
            if flushed:
                logger.info(f"[BOT] حُذفت {flushed} حزمة متبقية")

        self._bot_running = False

        if self.gs:
            hand_sz = self.gs.hand_size()
            logger.info(f"blocks_played={self._step}, blocks_in_hand={hand_sz}")

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
