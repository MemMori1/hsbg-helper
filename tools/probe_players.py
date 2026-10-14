#!/usr/bin/env python3
"""P1 探针：8 人座位与金币从日志怎么读。

P0 已确认 CREATE_GAME 段有 `Player EntityID=N PlayerID=M ...` 声明，
但一局战棋有 8 名玩家，而声明只有 4 条（且含 hi=0 lo=0 的空位）。
本探针回答：
  H6 一局里到底出现多少个不同的 PLAYER_ID / HERO_ENTITY
  H7 对手的血量/等级/技术等级存在哪个实体上、用什么 tag
  H8 金币怎么读（RESOURCES 很少；RESOURCES_USED 多）
  H9 玩家名与 PLAYER_ID 的对应关系
"""
import sys
import os
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from hsbg.parser import iter_file  # noqa: E402

DEFAULT = "/Applications/Hearthstone/Logs/Hearthstone_2026_10_10_21_11_09/Power.log"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    ent = {}                      # id -> {card, name, ZONE, tags...}
    player_ids = {}               # entity_id -> PLAYER_ID
    heroes = {}                   # entity_id -> HERO_ENTITY
    tag_counter = Counter()
    res_events = []               # (line, entity, tag, value)
    player_decl_lines = []
    turn = None
    n_player_decls = 0
    first_turn_done = False

    for ev in iter_file(path):
        if ev.kind == "PLAYER":
            n_player_decls += 1
            player_decl_lines.append((ev.line_no, ev.detail))
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
        elif ev.kind in ("TAG_CHANGE", "ATTACHED_TAG"):
            if ev.entity_id is not None:
                e = ent.setdefault(ev.entity_id, {})
                e[ev.tag] = ev.value
            tag_counter[ev.tag] += 1
            if ev.tag in ("RESOURCES", "RESOURCES_USED", "PLAYER_TECH_LEVEL",
                          "TECH_LEVEL", "DAMAGE", "HEALTH"):
                res_events.append((ev.line_no, ev.entity.name if ev.entity else ev.entity_id,
                                   ev.tag, ev.value))

        if ev.tag == "TURN" and ev.entity and ev.entity.name == "GameEntity":
            new = ev.value
            if turn is not None and new != turn:
                first_turn_done = True
                break
            turn = new

    print(f"=== H6: PLAYER 声明 {n_player_decls} 条 ===")
    for ln, d in player_decl_lines:
        print(f"  line {ln}: {d.strip()}")

    print("\n=== 一局里出现过的 PLAYER_ID / HERO_ENTITY ===")
    for i, e in sorted(ent.items()):
        if e.get("PLAYER_ID") or e.get("HERO_ENTITY"):
            print(f"  id={i:<5} PLAYER_ID={str(e.get('PLAYER_ID')):<5} "
                  f"HERO_ENTITY={str(e.get('HERO_ENTITY')):<5} "
                  f"card={str(e.get('card')):<24} name={e.get('name','')}")

    print("\n=== H7/H9: 玩家实体（有 hero 的）的属性 ===")
    for i, e in sorted(ent.items()):
        if e.get("HERO_ENTITY"):
            keys = ("PLAYER_ID", "HERO_ENTITY", "HEALTH", "DAMAGE", "TECH_LEVEL",
                    "PLAYER_TECH_LEVEL", "RESOURCES", "RESOURCES_USED",
                    "BACON_MAX_PLAYER_TECH_LEVEL", "BACON_DIED_LAST_COMBAT",
                    "BACON_WON_LAST_COMBAT", "BACON_IN_COMBAT_PHASE")
            attrs = {k: e.get(k) for k in keys if e.get(k) is not None}
            print(f"  id={i:<5} {e.get('name','')}: {attrs}")

    print("\n=== H8: 经济类 tag 出现次数 ===")
    for t in ("RESOURCES", "RESOURCES_USED", "PLAYER_TECH_LEVEL", "TECH_LEVEL",
              "HEALTH", "DAMAGE", "COST", "EXHAUSTED"):
        print(f"  {t:<26} {tag_counter.get(t, 0)}")

    print("\n=== H8: 第一局里 RESOURCES / RESOURCES_USED 的前 12 条 ===")
    shown = 0
    for ln, who, tag, val in res_events:
        if tag in ("RESOURCES", "RESOURCES_USED") and shown < 12:
            print(f"  line={ln:<7} {tag:<16} entity={str(who):<24} value={val}")
            shown += 1


if __name__ == "__main__":
    main()
