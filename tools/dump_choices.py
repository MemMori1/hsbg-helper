#!/usr/bin/env python3
"""把真机日志里**实际出现过**的四选一 / 三选一选项导出来。

战棋里的「选择」不在 Power 流里，而在 `GameState.DebugPrintEntityChoices`。
本工具扫全部日志，把下面三类选择里用户被 offer 过的 CardID 汇总出来：

  · 饰品      —— source 的 cardId 是 `BG30_Trinket_1st`（小型）/ `BG30_Trinket_2nd`（大型）等
  · 黑暗之赐  —— source 的 cardId 是 `BG36_MidGameEffect_010`
  · 起手换牌  —— kind 是 `MULLIGAN`

为什么要这个：饰品卡表里有 400+ 张，绝大多数是历史赛季的；只有日志能证明
"这张饰品这个版本真的会出现"。做饰品推荐时应该只在这份清单里挑。

用法：
    /usr/bin/python3 tools/dump_choices.py [-o 输出.json]
"""

import json
import os
import re
import sys
from collections import Counter, OrderedDict
from glob import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from hsbg.parser import iter_file      # noqa: E402

LOG_ROOT = "/Applications/Hearthstone/Logs"

_SRC_CARD_RE = re.compile(r"cardId=([A-Za-z0-9_]+)")

KIND_BY_SOURCE = (
    ("BG30_Trinket_1st", "trinket_small"),
    ("BG30_Trinket_2nd", "trinket_large"),
    ("BG36_MidGameEffect_010", "dark_gift"),
)


def classify(source, fallback_kind):
    if fallback_kind == "MULLIGAN":
        return "mulligan"
    cid = None
    if source:
        m = _SRC_CARD_RE.search(source)
        if m:
            cid = m.group(1)
    for prefix, name in KIND_BY_SOURCE:
        if cid == prefix:
            return name
    return None


def main(argv):
    out = os.path.join(ROOT, "data", "choices_current.json")
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]

    cards = json.load(open(os.path.join(ROOT, "data", "cards_zhCN.json")))["cards"]

    buckets = OrderedDict((k, Counter()) for k in
                          ("trinket_small", "trinket_large", "dark_gift", "mulligan"))
    games = 0
    for path in sorted(glob(os.path.join(LOG_ROOT, "*", "Power.log"))):
        chosen = set()
        cur_kind, cur_source = None, None
        before = {k: sum(v.values()) for k, v in buckets.items()}
        for ev in iter_file(path):
            if getattr(ev, "kind", None) == "CREATE_GAME":
                games += 1
                cur_kind, cur_source = None, None
            if getattr(ev, "kind", None) != "CHOICE":
                continue
            # 选项行的归属只能靠「当前正在收选项的块」——头行开新块，Source= 行补来源，
            # 选项行本身既不带 id 也不带 source，必须靠上面两个变量继承。
            if getattr(ev, "choice_kind", None):
                cur_kind, cur_source = ev.choice_kind, None
            if getattr(ev, "choice_source", None):
                cur_source = ev.choice_source
            opt = getattr(ev, "choice_option", None)
            if opt is None or not getattr(opt, "card_id", None):
                continue
            name = classify(cur_source, cur_kind)
            if name is None:
                continue
            # 同一局同一张只算一次（选项行会重复出现）
            key = (name, opt.card_id)
            if key in chosen:
                continue
            chosen.add(key)
            buckets[name][opt.card_id] += 1
        print("  %s -> %s" % (os.path.basename(os.path.dirname(path)),
                              ", ".join("%s +%d" % (k, sum(v.values()) - before[k])
                                        for k, v in buckets.items()
                                        if sum(v.values()) - before[k])))

    payload = OrderedDict()
    for name, counter in buckets.items():
        rows = []
        for cid, n in counter.most_common():
            c = cards.get(cid) or {}
            text = re.sub(r"<[^>]+>", "", c.get("text") or "").replace("\n", " ")
            rows.append({
                "card_id": cid,
                "name": c.get("name") or "",
                "offered_games": n,
                "type": c.get("type"),
                "text": text,
            })
        payload[name] = rows
    payload["_meta"] = {"games": games,
                        "note": "offered_games = 该选项在多少局里被提供过"}
    with open(out, "w") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("\n共扫描 %d 局 -> %s" % (games, out))
    for name in ("trinket_small", "trinket_large", "dark_gift"):
        print("  %-14s %d 种" % (name, len(payload[name])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
