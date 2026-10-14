#!/usr/bin/env python3
"""P1 探针：回合 1 的 MAIN_START 阶段里，商店 3 张是怎么"进场"的。

P0 快照显示商店随从和本地随从同在 ZONE=PLAY。要重建商店，必须找到
"商店刷新"这个事件本身——它应该是一批 ZONE→PLAY 或 CREATE 动作。

本探针按行号顺序打印回合 1 MAIN_START 期间所有涉及 BG* 随从的
FULL_ENTITY / SHOW_ENTITY / ZONE 变动，看它们的顺序与伴随 tag。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from hsbg.parser import iter_file  # noqa: E402

DEFAULT = "/Applications/Hearthstone/Logs/Hearthstone_2026_10_10_21_11_09/Power.log"

MECH = ("TB_BaconShop", "Bacon_", "Trinket", "Button", "BG_OldGod")


def interesting(card):
    return card.startswith("BG") and not any(m in card for m in MECH)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3200
    turn = None
    step = ""
    started = False

    for ev in iter_file(path):
        if ev.line_no > limit:
            break
        if ev.tag == "STEP":
            step = ev.value
            print(f"  >>> [line {ev.line_no}] STEP = {ev.value}")
            continue
        if ev.tag == "TURN" and ev.entity and ev.entity.name == "GameEntity":
            turn = ev.value
            print(f"\n=== [line {ev.line_no}] TURN = {ev.value} ===")
            continue

        card = ev.card_id or (ev.entity.card_id if ev.entity else None) or ""
        if not interesting(card):
            # 也报一下"商店刷新按钮"之类的机制事件，帮助定位
            if card.startswith("TB_BaconShop") and ev.kind in ("TAG_CHANGE",) and ev.tag == "ZONE":
                print(f"      [line {ev.line_no}] MECH {card} ZONE={ev.value}")
            continue

        if ev.kind in ("FULL_ENTITY", "SHOW_ENTITY", "CHANGE_ENTITY"):
            z = ev.entity.zone if ev.entity else None
            print(f"      [line {ev.line_no}] {ev.kind:<13} id={ev.entity_id:<5} "
                  f"card={card:<14} ZONE={z} pos={ev.entity.zone_pos if ev.entity else None} "
                  f"ctrl={ev.entity.player if ev.entity else None}")
        elif ev.kind in ("TAG_CHANGE", "ATTACHED_TAG") and ev.tag == "ZONE":
            print(f"      [line {ev.line_no}] TAG_CHANGE    id={ev.entity_id:<5} "
                  f"card={card:<14} ZONE={ev.value}")


if __name__ == "__main__":
    main()
