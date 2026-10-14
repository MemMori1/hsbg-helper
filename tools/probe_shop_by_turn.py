#!/usr/bin/env python3
"""P1 探针：用「商店随从的 CONTROLLER == 鲍勃的 PLAYER_ID」这条判据，
逐回合重建商店，并检查是否恒为 3 张、位置是否 1..N。

用法： python3 tools/probe_shop_by_turn.py [Power.log 路径]
"""
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from hsbg.parser import iter_file  # noqa: E402

DEFAULT = "/Applications/Hearthstone/Logs/Hearthstone_2026_10_10_21_11_09/Power.log"

# 商店侧机制卡/按钮，不算"可买的随从"
MECH = ("TB_BaconShop", "Bacon_", "Trinket", "Button", "BG_OldGod", "_HERO_", "_HP_")


def buyable(card: str) -> bool:
    if not card.startswith("BG"):
        return False
    if any(m in card for m in MECH):
        return False
    # 英雄技能附魔/衍生牌常见后缀
    if card.endswith("e") or card.endswith("p"):
        return False
    return True


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    ent = {}
    turn = None
    step = ""
    bob_pid = None
    local_pid = None
    seats = {}          # PLAYER_ID -> hero card
    timeline = []       # (turn, step, shop list)

    for ev in iter_file(path):
        if ev.kind in ("FULL_ENTITY", "SHOW_ENTITY", "PLAYER"):
            if ev.entity_id is None:
                continue
            e = ent.setdefault(ev.entity_id, {})
            if ev.card_id:
                e["card"] = ev.card_id
            if ev.entity and ev.entity.name:
                e["name"] = ev.entity.name
            if ev.entity and ev.entity.zone:
                e["ZONE"] = ev.entity.zone
            if ev.entity and ev.entity.player is not None:
                e["CONTROLLER"] = str(ev.entity.player)
            for k, v in (ev.tags or {}).items():
                e[k] = v
        elif ev.kind in ("TAG_CHANGE", "ATTACHED_TAG") and ev.entity_id is not None:
            ent.setdefault(ev.entity_id, {})[ev.tag] = ev.value

        # 认出鲍勃：card == TB_BaconShopBob 所在玩家的 PLAYER_ID
        for i, e in ent.items():
            if e.get("card") == "TB_BaconShopBob" and e.get("CONTROLLER"):
                bob_pid = e["CONTROLLER"]

        if ev.tag == "STEP":
            step = ev.value
            if step == "MAIN_READY":
                shop = []
                for i, e in ent.items():
                    c = e.get("card")
                    if not c or not buyable(c):
                        continue
                    if e.get("ZONE") != "PLAY":
                        continue
                    if bob_pid is not None and str(e.get("CONTROLLER")) == str(bob_pid):
                        pos = e.get("ZONE_POSITION")
                        shop.append((int(pos) if str(pos).isdigit() else 99, c, e.get("name", ""), i,
                                     e.get("TECH_LEVEL"), e.get("ATK"), e.get("HEALTH")))
                timeline.append((turn, step, sorted(shop)))
        if ev.tag == "TURN" and ev.entity and ev.entity.name == "GameEntity":
            turn = ev.value

    print(f"鲍勃的 PLAYER_ID = {bob_pid}\n")
    for t, st, shop in timeline[:14]:
        print(f"=== 回合 {t} {st}：商店 {len(shop)} 张 ===")
        for pos, c, name, i, tech, atk, hp in shop:
            print(f"    pos={pos:<3} id={i:<5} {c:<14} {name:<12} "
                  f"tech={tech} {atk}/{hp}")


if __name__ == "__main__":
    main()
