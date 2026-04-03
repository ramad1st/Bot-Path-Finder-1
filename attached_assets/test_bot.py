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
if __name__ == "__main__":
    if len(sys.argv) >= 2:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        # ضع بياناتك هنا مباشرة أو مرّر ملف JSON
        print("استخدم: python test_bot.py data.json")
        print("\nشكل الملف المطلوب:")
        print(json.dumps({
            "pile_blocks": [
                {"id": 1, "col": 100, "row": 80, "layer": 0, "type": 5},
                {"id": 2, "col": 110, "row": 80, "layer": 0, "type": 5},
                {"id": 3, "col": 120, "row": 80, "layer": 1, "type": 5},
            ],
            "hand_blocks":    [],
            "storage_blocks": []
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    simulate(data)
