#!/usr/bin/env python3
"""P1 探针：DragBuy 与非零 2442 出现的时机，对照 STEP 序列。

上一版探针只在 MAIN_READY 抓快照，结果大多数回合是 0 张。
本探针改为：每次 2442 变成非零或归零时就报一行，带当前 turn/step，
从而看出"商店在哪个 STEP 是有效的"。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from hsbg.parser import iter_file  # noqa: E402

DEFAULT = "/Applications/Hearthstone/Logs/Hearthstone_2026_10_10_21_11_09/Power.log"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    ent = {}
    turn = None
    step = ""
    drag = {}
    shown = 0

    for ev in iter_file(path):
        if ev.kind in ("FULL_ENTITY", "SHOW_ENTITY", "PLAYER"):
            if ev.entity_id is None:
                continue
            e = ent.setdefault(ev.entity_id, {})
            if ev.card_id:
                e["card"] = ev.card_id
            if ev.entity and ev.entity.name:
                e["name"] = ev.entity.name
            for k, v in (ev.tags or {}).items():
                e[k] = v
        elif ev.kind in ("TAG_CHANGE", "ATTACHED_TAG") and ev.entity_id is not None:
            ent.setdefault(ev.entity_id, {})[ev.tag] = ev.value

        if ev.tag == "STEP":
            step = ev.value
        if ev.tag == "TURN" and ev.entity and ev.entity.name == "GameEntity":
            turn = ev.value

        if ev.tag == "2442" and ev.entity_id is not None:
            old = drag.get(ev.entity_id)
            drag[ev.entity_id] = ev.value
            if old == ev.value:
                continue
            if shown < 60:
                live = [v for v in drag.values() if v and str(v) != "0"]
                tgt = ent.get(int(ev.value)) if ev.value and str(ev.value) != "0" else None
                print(f"line={ev.line_no:<7} turn={turn:<3} step={step:<18} "
                      f"drag={ev.entity_id:<6} 2442: {old} -> {ev.value:<6} "
                      f"在售={len(live)} "
                      f"{('-> ' + str(tgt.get('card')) + ' ' + str(tgt.get('name'))) if tgt else ''}")
                shown += 1


if __name__ == "__main__":
    main()
