#!/usr/bin/env python3
"""把一整局的「决策序列」打出来，用来对照大神的运营思路复盘规则引擎。

用法：
    ./tools/trace_decisions.py <Power.log 路径>

每个招募回合一行：回合 / 满金 / 酒馆等级 / 场面 / **决策框的那一个动作** /
触发原因 / 理由。也就是悬浮框上方那个独立小窗逐回合会显示什么。

取样时刻 = 该回合刚进入 `MAIN_ACTION` 的那一瞬（`RESOURCES` 已发、还没花钱），
这正是"该做决定了"的时刻。

⚠️ 两个坑（都踩过）：
  1. 回合/阶段必须在**事件流**里判（`TAG_CHANGE GameEntity tag=TURN` 与
     `tag=STEP`），**不能**每个事件都读 `builder.state` —— 读一次就触发一次
     全量惰性刷新，整局会退化成 O(事件×实体)，从 2 秒变成 2 分钟。
  2. 同一回合只取第一个 `MAIN_ACTION`，别取最后一个（那时金币已经花掉了）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "src"))

from hsbg.cards import CardResolver  # noqa: E402
from hsbg.parser import iter_file  # noqa: E402
from hsbg.rules import RulesEngine  # noqa: E402
from hsbg.state import StateBuilder  # noqa: E402


def trace(path, db, db_en=None):
    """返回 [(turn, gold, tier, board, decision, best_name, best_score)]。"""
    engine = RulesEngine(db, db_en=db_en)
    builder = StateBuilder()
    turn, step, seen, rows = 0, "", set(), []
    for ev in iter_file(path):
        if ev.tag == "TURN" and ev.entity is not None and ev.entity.name == "GameEntity":
            try:
                turn = int(ev.value)
            except (TypeError, ValueError):
                pass
        elif ev.tag == "STEP":
            step = str(ev.value)
        builder.apply(ev)
        if step != "MAIN_ACTION" or turn in seen:
            continue
        seen.add(turn)
        st = builder.state
        seat = st.local_seat
        if seat is None:
            continue
        rep = engine.evaluate(st)
        best = max(rep.shop, key=lambda x: x.score, default=None)
        rows.append((turn, seat.gold, seat.tech_level, len(st.board),
                     rep.decision, best.name if best else "", best.score if best else None))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--zh", action="store_true", help="只加载中文卡表（默认双语）")
    args = ap.parse_args(argv)

    resolver = CardResolver.load()
    rows = trace(args.path, resolver.db,
                 None if args.zh else getattr(resolver, "db_en", None))

    print("%-5s %-5s %-4s %-5s %-14s %-15s %-9s %s"
          % ("回合", "满金", "本", "场面", "决策框", "触发", "店最高", "理由"))
    print("-" * 104)
    for turn, gold, tier, board, d, best, score in rows:
        cost = ("  %s" % d.cost_text) if d.cost_text else ""
        print("%-5d %-5s %-4s %-5d %-14s %-15s %-9s %s"
              % (turn, gold if gold is not None else "-",
                 tier if tier is not None else "-", board,
                 d.headline + cost, d.trigger,
                 ("%.1f %s" % (score, best[:6])) if score is not None else "-",
                 d.reason[:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
