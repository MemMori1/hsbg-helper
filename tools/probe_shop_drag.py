#!/usr/bin/env python3
"""P1 探针（决定性）：用 TB_BaconShop_DragBuy 的 tag 2442 重建商店。

tag 2442 是 DragBuy（"可拖拽购买"的悬停目标）指向真实商店随从实体的引用。
每次刷新会生成一批 DragBuy，各带一个 2442，卖出/清空时置 0。
因此「2442 != 0 的 DragBuy 集合」= 当前商店在售随从。

用法： python3 tools/probe_shop_drag.py [Power.log 路径]
"""
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from hsbg.parser import iter_file  # noqa: E402

DEFAULT = "/Applications/Hearthstone/Logs/Hearthstone_2026_10_10_21_11_09/Power.log"
DRAG_BUY = "TB_BaconShop_DragBuy"
TAG_REF = "2442"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    ent = {}
    turn = None
    step = ""
    drag = {}                 # dragbuy entity_id -> target entity_id
    rows = []                 # (turn, step, [ (slot_order, target_id, card, name, tech, atk, hp) ])

    def snapshot(reason):
        shop = []
        for did, tid in sorted(drag.items()):
            if not tid or str(tid) == "0":
                continue
            t = ent.get(tid) or ent.get(int(tid))
            if t is None:
                continue
            shop.append((did, int(tid), t.get("card", ""), t.get("name", ""),
                         t.get("TECH_LEVEL"), t.get("ATK"), t.get("HEALTH")))
        rows.append((turn, step, reason, shop))

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
            for k, v in (ev.tags or {}).items():
                e[k] = v
        elif ev.kind in ("TAG_CHANGE", "ATTACHED_TAG") and ev.entity_id is not None:
            e = ent.setdefault(ev.entity_id, {})
            e[ev.tag] = ev.value

        if ev.tag == TAG_REF and ev.entity_id is not None:
            drag[ev.entity_id] = ev.value

        if ev.tag == "STEP":
            step = ev.value
            if step == "MAIN_READY":
                snapshot("MAIN_READY")
        if ev.tag == "TURN" and ev.entity and ev.entity.name == "GameEntity":
            turn = ev.value

    print(f"共 {len(rows)} 个 MAIN_READY 快照\n")
    for turn_, st, reason, shop in rows:
        print(f"=== 回合 {turn_} {st}（{reason}）：商店 {len(shop)} 张 ===")
        for did, tid, card, name, tech, atk, hp in shop:
            print(f"    dragbuy={did:<5} -> id={tid:<5} {card:<16} {name:<14} "
                  f"tech={tech} {atk}/{hp}")


if __name__ == "__main__":
    main()
