#!/usr/bin/env python3
"""把**真机日志里实际出现过**的战棋随从导出来，作为"当前赛季牌池"的实证快照。

为什么要从日志导而不是从卡表导：卡表里 5,787 张战棋卡是**所有赛季的并集**
（BG19 到 BG36 都在），而当前赛季实际只会用到其中一个子集。日志是唯一能证明
"这张牌这个版本真的能用"的来源。

输出两个集合：
  · `shop`  —— 出现在 `TB_BaconShop_DragBuy` 槽位（= 真的在酒馆里卖）
  · `board` —— 出现在任意玩家场面上

用法：
    /usr/bin/python3 tools/dump_pool.py [-o 输出.json]
"""

import json
import os
import sys
from glob import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from hsbg.parser import iter_file                      # noqa: E402
from hsbg.state import CARD_DRAG_BUY, CARD_DRAG_BUY_SPELL  # noqa: E402

LOG_ROOT = "/Applications/Hearthstone/Logs"


def main(argv):
    out = "pool_current.json"
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]

    cards = json.load(open(os.path.join(ROOT, "data", "cards_zhCN.json")))["cards"]

    def is_pool_minion(cid):
        c = cards.get(cid)
        if c is None or c.get("type") != "MINION":
            return False
        return c.get("techLevel") is not None

    shop, board, hosts = set(), set(), {}
    for path in sorted(glob(os.path.join(LOG_ROOT, "*", "Power.log"))):
        dragbuy = {}            # 槽位宿主 entity_id -> 2442 指向的随从 entity_id
        by_eid = {}             # entity_id -> card_id
        for ev in iter_file(path):
            # 实体 → CardID 的映射：`ev.entity` 是 EntityRef，带 card_id 快照
            if ev.entity_id is not None and ev.entity is not None and ev.entity.card_id:
                by_eid[ev.entity_id] = ev.entity.card_id
            if ev.entity_id is not None and ev.card_id:
                by_eid[ev.entity_id] = ev.card_id

            # 槽位宿主上的 `tag=2442` 指向真正可买的随从；归 0 = 买走 / 刷新
            if ev.tag == "2442" and ev.entity_id is not None:
                host = by_eid.get(ev.entity_id)
                if host in (CARD_DRAG_BUY, CARD_DRAG_BUY_SPELL):
                    try:
                        newv = int(ev.value)
                    except (TypeError, ValueError):
                        newv = 0
                    if newv:
                        dragbuy[ev.entity_id] = newv
                    else:
                        cid = by_eid.get(dragbuy.pop(ev.entity_id, None))
                        if cid and is_pool_minion(cid):
                            shop.add(cid)

            # 场面：ZONE=PLAY 的随从（含对手）
            if ev.tag == "ZONE" and str(ev.value) == "PLAY" and ev.entity_id is not None:
                cid = by_eid.get(ev.entity_id)
                if cid and is_pool_minion(cid):
                    board.add(cid)
        print("  %s -> shop %d / board %d" % (os.path.basename(os.path.dirname(path)),
                                              len(shop), len(board)))

    def row(cid):
        c = cards[cid]
        return {
            "card_id": cid,
            "name": c.get("name"),
            "tech_level": c.get("techLevel"),
            "attack": c.get("attack"),
            "health": c.get("health"),
            "races": c.get("races") or [],
            "mechanics": [m for m in (c.get("mechanics") or [])
                          if m != "TRIGGER_VISUAL"],
            "text": (c.get("text") or "").replace("\n", " "),
        }

    payload = {
        "shop": [row(c) for c in sorted(shop, key=lambda c: (cards[c].get("techLevel") or 0, c))],
        "board_only": [row(c) for c in sorted(board - shop,
                                              key=lambda c: (cards[c].get("techLevel") or 0, c))],
    }
    with open(out, "w") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("酒馆里出现过 %d 种随从；只在场面出现过 %d 种 -> %s"
          % (len(payload["shop"]), len(payload["board_only"]), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
