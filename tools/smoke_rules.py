#!/usr/bin/env python3
"""rules.py 真机冒烟：跑真实日志到某一行为止，打印完整建议报告。

用法::

    ./hsbg-helper/tools/smoke_rules.py [--until-line N] [--log PATH]

零第三方依赖。卡表默认读 `data/cards_zhCN.json`。
"""
import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from hsbg.cards import CardDB, DEFAULT_CARD_DB_PATH          # noqa: E402
from hsbg.config import DEFAULT_LOG_ROOT                     # noqa: E402
from hsbg.logs import latest_session                         # noqa: E402
from hsbg.parser import PowerLogParser                       # noqa: E402
from hsbg.rules import RulesEngine                           # noqa: E402
from hsbg.state import StateBuilder                          # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=None)
    ap.add_argument("--db", default=str(DEFAULT_CARD_DB_PATH))
    ap.add_argument("--until-line", type=int, default=92000)
    args = ap.parse_args(argv)

    log = args.log
    if log is None:
        sess = latest_session(DEFAULT_LOG_ROOT)
        if sess is None:
            print("没找到会话目录", file=sys.stderr)
            return 1
        log = str(sess.power_log)
    print("日志:", log)

    db = CardDB.load(args.db)
    print("卡表:", db.stats())

    parser = PowerLogParser()
    builder = StateBuilder()
    t0 = time.time()
    n = 0
    with open(log, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            n += 1
            if args.until_line and n > args.until_line:
                break
            for ev in parser.feed_line(line):
                builder.apply(ev)
    for ev in parser.flush():
        builder.apply(ev)
    print("解析 lines=%d 用时 %.2fs" % (n, time.time() - t0))

    st = builder.state
    print("turn=%s step=%s local=%s" % (st.turn, st.step, st.local_player_id))
    rep = RulesEngine(db).evaluate(st)
    print("headline:", rep.headline)
    print("gold=%s hp=%s tier=%s board=%d hand=%d" % (
        rep.gold, rep.health, rep.tavern_tier, rep.board_size, rep.hand_size))
    print("--- 商店打分 ---")
    for s in rep.shop:
        print("  %d. %-16s %6.1f分 %-4s %s" % (
            s.index, s.name, s.score, s.action, "; ".join(s.reasons)))
    print("--- 三连候选 ---")
    for t in rep.triples:
        print("  %s 已有 %d 张 商店%s" % (t.name, t.owned, "有" if t.in_shop else "无"))
    print("--- 升本 ---")
    lv = rep.level
    print("  %s本 -> %s本 费用%s 买得起=%s %.1f分 %s | %s" % (
        lv.current_tier, lv.next_tier, lv.cost, lv.affordable, lv.score,
        lv.action, "; ".join(lv.reasons)))
    print("--- 预警 ---")
    for w in rep.warnings:
        print("  [%-6s] %s" % (w.level, w.text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
