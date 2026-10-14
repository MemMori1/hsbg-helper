#!/usr/bin/env python3
"""P1 探针：商店（酒馆）里的 3 张随从怎么从日志里认出来。

P0 已确认：商店随从和本地随从都在 ZONE=PLAY，区别只在 CONTROLLER
（本地是 6，商店侧是 14）。但 14 是"鲍勃"还是"某个对手"必须分清，
否则 P1 会把商店当对手场面。

判据假设（逐个验证）：
  H1 商店随从的 CONTROLLER 恒等于 TB_BaconShopBob 的 CONTROLLER
  H2 商店随从只在刷卡阶段出现在 PLAY，战斗阶段消失
  H3 ZONE_POSITION 只覆盖 1..7（酒馆位），本地场面另有一套编号
  H4 TB_BaconShop_8P_PlayerE 是"鲍勃"的玩家实体，其 PLAYER_ID 是特殊值
  H5 商店随从带 TECH_LEVEL 且等于当期酒馆等级

用法： python3 tools/probe_shop.py [Power.log 路径]
"""
import sys
import os
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from hsbg.parser import iter_file  # noqa: E402

DEFAULT = "/Applications/Hearthstone/Logs/Hearthstone_2026_10_10_21_11_09/Power.log"

# 商店侧会出现的"机制卡"前缀，用来把真随从和 UI 假实体分开
MECH_PREFIXES = (
    "TB_BaconShop", "Bacon_", "BG30_Trinket", "BG36_Button",
    "TB_Bacon_", "BG_OldGod",
)


def is_minion_like(card_id: str) -> bool:
    """粗判：BG* 开头、不是英雄/技能/饰品/按钮。"""
    if not card_id.startswith("BG"):
        return False
    if "_HERO_" in card_id or card_id.endswith("p") or card_id.endswith("e"):
        return False
    if "Trinket" in card_id or "Button" in card_id:
        return False
    return True


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    ent = {}          # id -> 属性表
    turn = None
    step = ""
    bob_ctrl = None
    bob_player_id = None
    ctrl_seen = Counter()
    # 每个 STEP 阶段里，PLAY 区带 pos 的随从各是谁
    by_step = defaultdict(list)
    zone_hist = defaultdict(Counter)   # card -> ZONE 出现次数
    pos_range = defaultdict(set)

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
            if ev.tag == "ZONE":
                card = e.get("card")
                if card:
                    zone_hist[card][ev.value] += 1

        if ev.tag == "STEP":
            step = ev.value
        if ev.tag == "TURN" and ev.entity and ev.entity.name == "GameEntity":
            new = ev.value
            if turn is not None and new != turn:
                break
            turn = new

        # 每个"活动"阶段抓一张 PLAY 区快照
        if ev.kind == "TAG_CHANGE" and ev.tag == "CURRENT_PLAYER" and ev.value == "1":
            snap = []
            for i, e in ent.items():
                c = e.get("card")
                if not c or e.get("ZONE") != "PLAY":
                    continue
                if not is_minion_like(c):
                    continue
                pos = e.get("ZONE_POSITION")
                snap.append((int(pos) if str(pos).isdigit() else None,
                             int(e["CONTROLLER"]) if str(e.get("CONTROLLER", "")).isdigit() else None,
                             c, e.get("name", ""), i))
            by_step[step or "?"].append(snap)

    # ---- 输出 ----
    print("=== H1: CONTROLLER 取值分布（有 card 的实体）===")
    for i, e in ent.items():
        if e.get("CONTROLLER"):
            ctrl_seen[str(e["CONTROLLER"])] += 1
    print("  ", dict(ctrl_seen.most_common()))

    for i, e in ent.items():
        if e.get("card") == "TB_BaconShopBob":
            bob_ctrl = e.get("CONTROLLER")
            bob_player_id = e.get("PLAYER_ID")
    print(f"  TB_BaconShopBob: id 的 controller={bob_ctrl}  PLAYER_ID={bob_player_id}")
    for i, e in ent.items():
        if e.get("card") == "TB_BaconShop_8P_PlayerE":
            print(f"  TB_BaconShop_8P_PlayerE: controller={e.get('CONTROLLER')} "
                  f"PLAYER_ID={e.get('PLAYER_ID')} ZONE={e.get('ZONE')}")
    # 本地玩家是谁
    for i, e in ent.items():
        if e.get("PLAYER_ID") and e.get("HERO_ENTITY"):
            print(f"  Player id={i} PLAYER_ID={e.get('PLAYER_ID')} "
                  f"HERO_ENTITY={e.get('HERO_ENTITY')} ctrl={e.get('CONTROLLER')}")

    print("\n=== H5: PLAY 区带 ZONE_POSITION 的随从，按合法阶段分组 ===")
    for st, snaps in by_step.items():
        if not snaps:
            continue
        print(f"  --- 阶段 {st}（{len(snaps)} 次快照）---")
        last = snaps[-1]
        for pos, ctrl, card, name, i in sorted(last, key=lambda t: (t[0] is None, t[0])):
            print(f"      pos={str(pos):<4} ctrl={str(ctrl):<4} id={i:<5} {card:<16} {name}")

    print("\n=== H2: 随从卡出现过的 ZONE ===")
    for card, cnt in sorted(zone_hist.items()):
        if is_minion_like(card):
            print(f"  {card:<18} {dict(cnt)}")

    print("\n=== H3: 每个随从用过的 ZONE_POSITION ===")
    for i, e in ent.items():
        c = e.get("card")
        if c and is_minion_like(c) and e.get("ZONE_POSITION"):
            pos_range[c].add(str(e["ZONE_POSITION"]))
    for card, pos in sorted(pos_range.items()):
        print(f"  {card:<18} {sorted(pos)}")


if __name__ == "__main__":
    main()
