#!/usr/bin/env python3
"""用**真人真机日志**校准商店买牌决策。

出发点：`_score_shop` 里 `买` 要 ≥18 分，但实测早期随从最高只有 5.8~9.1 分，
怀疑阈值拍得不对。这个工具把它换成**实测数据**：每个回合拍一张商店快照并打分，
再对齐真人那回合**实际买了什么 / 升没升本**，看分数到底能不能预测真人的决定。

买牌信号（已实测）：槽位引用置 0，且被指向实体的 `ZONE == "HAND"`、
`CONTROLLER == 本地玩家`。刷新时未买的那些是 `ZONE == "REMOVEDFROMGAME"`。

⚠️ 两个坑（都踩过）：
  1. `builder.state` 是**惰性刷新**的 property，每个事件读一次会退化成 O(n²)
     （单份 92 MB 日志从 ~8 s 涨到 138 s）。所以先用便宜条件筛，再取 state。
  2. 一份日志里有**多局游戏**，实体 id 与 TURN 都会重置。快照必须按
     `(局号, 回合)` 做键，否则第 2 局的买入会被算到第 1 局头上。

用法：
    /usr/bin/python3 tools/calibrate_shop.py [日志...]
不给路径时扫 `/Applications/Hearthstone/Logs` 下所有会话。
"""

from __future__ import annotations

import os
import sys
import glob
import collections

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from hsbg.cards import CardResolver  # noqa: E402
from hsbg.parser import iter_file  # noqa: E402
from hsbg.rules import RulesEngine  # noqa: E402
from hsbg.state import StateBuilder, CARD_DRAG_BUY, CARD_DRAG_BUY_SPELL  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LOG_ROOT = os.path.expanduser("/Applications/Hearthstone/Logs")

BUCKETS = [(0, 4), (4, 7), (7, 10), (10, 13), (13, 16), (16, 19), (19, 22), (22, 99)]


def default_logs():
    return sorted(glob.glob(os.path.join(LOG_ROOT, "*", "Power.log")))


def load_engine():
    res = CardResolver.load(
        os.path.join(ROOT, "data", "cards_zhCN.json"),
        os.path.join(ROOT, "data", "names_zhCN.json"),
        os.path.join(ROOT, "data", "cards_enUS.json"),
    )
    return RulesEngine(res.db, db_en=res.db_en)


def scan(path, engine):
    """扫一份日志，返回 (快照列表, 买入列表)。"""
    builder = StateBuilder()
    turn, step, game = 0, "", 0
    snapshots = {}          # (game, turn) -> snapshot
    bought = []
    live_ref = {}           # entity_id -> (game, turn)
    local_id = 6

    for ev in iter_file(path):
        if ev.kind == "CREATE_GAME":
            game += 1
            turn = 0
            live_ref.clear()
        elif ev.tag == "TURN" and ev.entity is not None and ev.entity.name == "GameEntity":
            try:
                turn = int(ev.value)
            except (TypeError, ValueError):
                pass
        elif ev.tag == "STEP":
            step = str(ev.value)

        # ⚠️ 见文件头坑 1：先筛再做（很少命中），不要每个事件都读 state
        snapshot_now = step == "MAIN_ACTION" and (game, turn) not in snapshots
        hand_now = ev.tag == "ZONE" and str(ev.value) == "HAND"
        builder.apply(ev)
        if not (snapshot_now or hand_now):
            continue

        st = builder.state
        seat = st.local_seat
        if seat is not None:
            local_id = seat.player_id

        # ① 每回合第一次进 MAIN_ACTION 时拍一张商店快照
        if snapshot_now and seat is not None:
            rep = engine.evaluate(st)
            by_slot = {s.slot_entity_id: s for s in st.shop_minions}
            items = {}
            for it in rep.shop:
                slot = by_slot.get(it.slot_entity_id)
                if slot is not None and slot.minion_entity_id is not None:
                    items[slot.minion_entity_id] = it
                    live_ref[slot.minion_entity_id] = (game, turn)
            # 法术槽位也要登记，否则「买了法术」会被误判成「这回合什么都没做」
            spells = {}
            for sp in st.shop_spells:
                if sp.minion_entity_id is not None:
                    spells[sp.minion_entity_id] = sp
                    live_ref[sp.minion_entity_id] = (game, turn)
            snapshots[(game, turn)] = {
                "key": (game, turn), "turn": turn, "gold": seat.gold,
                "tier": seat.tech_level, "board": len(st.board),
                "items": items, "spells": spells,
            }

        # ② 买牌信号：实体进手牌且控制者是本地玩家
        if hand_now and ev.entity_id is not None:
            ent = st.entities.get(ev.entity_id)
            if ent is None or not ent.card_id:
                continue
            if ent.card_id in (CARD_DRAG_BUY, CARD_DRAG_BUY_SPELL):
                continue
            ctrl = ent.tags.get("CONTROLLER")
            if ctrl is None or int(ctrl) != local_id:
                continue
            ref = live_ref.get(ev.entity_id)
            if ref is None:
                continue                      # 不是从商店买的（三连奖励/发现等）
            snap = snapshots.get(ref) or {}
            item = (snap.get("items") or {}).get(ev.entity_id)
            bought.append({"key": ref, "turn": ref[1], "minion_eid": ev.entity_id,
                           "in_snapshot": item is not None, "item": item,
                           "spell": ev.entity_id in (snap.get("spells") or {})})

    return [snapshots[k] for k in sorted(snapshots)], bought


def bucket_of(score):
    for lo, hi in BUCKETS:
        if lo <= score < hi:
            return "%2d-%2d" % (lo, hi)
    return "??"


def _q(values, frac):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * frac))]


def main(argv=None):
    logs = (argv or sys.argv[1:]) or default_logs()
    if not logs:
        print("找不到日志：%s" % LOG_ROOT, file=sys.stderr)
        return 1
    engine = load_engine()

    all_items, all_bought, all_snaps = [], [], []
    for path in logs:
        snaps, bought = scan(path, engine)
        all_snaps += snaps
        all_items += [it for s in snaps for it in s["items"].values()]
        all_bought += bought
        n_minion = sum(1 for b in bought if b["in_snapshot"])
        print("%-46s 商店 %3d · 买入 %3d（随从 %3d / 法术 %3d）" % (
            os.path.basename(os.path.dirname(path)), len(snaps), len(bought),
            n_minion, len(bought) - n_minion))

    by_key = collections.defaultdict(list)
    for b in all_bought:
        by_key[b["key"]].append(b)

    print()
    print("=" * 78)
    print("【一】单张随从分数 vs 真人买不买（分数能不能区分？）")
    in_snap = [b for b in all_bought if b["in_snapshot"]]
    hist = collections.defaultdict(lambda: [0, 0])
    for it in all_items:
        hist[bucket_of(it.score)][1] += 1
    for b in in_snap:
        hist[bucket_of(b["item"].score)][0] += 1
    print("  %-8s %6s %6s %8s" % ("分数段", "买到", "总数", "买入率"))
    for lo, hi in BUCKETS:
        key = "%2d-%2d" % (lo, hi)
        got, tot = hist.get(key, [0, 0])
        if tot:
            print("  %-8s %6d %6d %7.1f%%   %s" % (
                key, got, tot, 100.0 * got / tot,
                "#" * int(round(100.0 * got / tot / 2.5))))
    by_action = collections.defaultdict(lambda: [0, 0])
    for it in all_items:
        by_action[it.action][1] += 1
    for b in in_snap:
        by_action[b["item"].action][0] += 1
    print("  现行规则动作（买 ≥18 / 考虑 ≥11）：")
    for action in ("买", "考虑", "跳过"):
        got, tot = by_action.get(action, [0, 0])
        if tot:
            print("    %-4s 买到 %3d / 共 %3d = %5.1f%%" % (
                action, got, tot, 100.0 * got / tot))

    print()
    print("=" * 78)
    print("【二】真人是在店里**挑最高的**，不是按绝对分数线买")
    ranks = []
    for snap in all_snaps:
        items = sorted(snap["items"].values(), key=lambda i: -i.score)
        if len(items) < 2:
            continue
        eids = {b["minion_eid"] for b in by_key.get(snap["key"], [])
                if b["in_snapshot"]}
        for eid, it in snap["items"].items():
            if eid in eids:
                ranks.append((snap["turn"], items.index(it) + 1,
                              it.score, items[0].score))
    if ranks:
        for label, lo, hi in (("早期 1-4 回合", 1, 4), ("中期 5-9 回合", 5, 9),
                              ("后期 10+ 回合", 10, 999)):
            sub = [r for r in ranks if lo <= r[0] <= hi]
            if sub:
                print("  %-14s n=%3d  买中第1名 %5.1f%%  前2名 %5.1f%%  平均排名 %.2f" % (
                    label, len(sub),
                    100.0 * sum(1 for r in sub if r[1] == 1) / len(sub),
                    100.0 * sum(1 for r in sub if r[1] <= 2) / len(sub),
                    sum(r[1] for r in sub) / len(sub)))
        print("  %-14s n=%3d  买中第1名 %5.1f%%  前2名 %5.1f%%  平均排名 %.2f" % (
            "全部", len(ranks),
            100.0 * sum(1 for r in ranks if r[1] == 1) / len(ranks),
            100.0 * sum(1 for r in ranks if r[1] <= 2) / len(ranks),
            sum(r[1] for r in ranks) / len(ranks)))

    print()
    print("=" * 78)
    print("【三】商店级别：用「店内最高分」预测这回合会不会买")
    shops = []
    for snap in all_snaps:
        items = sorted(snap["items"].values(), key=lambda i: -i.score)
        if not items:
            continue
        bs = by_key.get(snap["key"], [])
        shops.append({
            "key": snap["key"], "turn": snap["turn"], "gold": snap["gold"],
            "board": snap["board"], "top": items[0].score,
            "bought": any(b["in_snapshot"] for b in bs),
            "any_buy": bool(bs), "spell": any(b["spell"] for b in bs),
        })
    if shops:
        n_buy = sum(1 for s in shops if s["bought"])
        print("  共 %d 个商店快照，其中 %d 个买了随从（%.0f%%）" % (
            len(shops), n_buy, 100.0 * n_buy / len(shops)))
        bt = [s["top"] for s in shops if s["bought"]]
        nt = [s["top"] for s in shops if not s["bought"]]
        if bt and nt:
            print("  买了的店最高分  p25=%.1f p50=%.1f p75=%.1f" % (
                _q(bt, .25), _q(bt, .5), _q(bt, .75)))
            print("  没买的店最高分  p25=%.1f p50=%.1f p75=%.1f   ← 两者接近="
                  "最高分没有预测力" % (_q(nt, .25), _q(nt, .5), _q(nt, .75)))
        print("  %-8s %6s %6s %8s %8s" % ("阈值", "命中", "误报", "召回", "精确"))
        for t in (0, 6, 8, 10, 12, 14, 16, 18, 20, 24):
            tp = sum(1 for s in shops if s["bought"] and s["top"] >= t)
            fp = sum(1 for s in shops if not s["bought"] and s["top"] >= t)
            print("  %-8s %6d %6d %7.0f%% %7.0f%%" % (
                ">=%d" % t, tp, fp,
                100.0 * tp / n_buy if n_buy else 0.0,
                100.0 * tp / (tp + fp) if (tp + fp) else 0.0))
        print("  ↑ 精确率若在各阈值上都贴着基础率（%.0f%%）不动，说明"
              "**调阈值没用**，问题在分数本身" % (100.0 * n_buy / len(shops)))

    print()
    print("=" * 78)
    print("【五】店内**第 1 名**按分数段的买入率（= 「推荐买最好的那张」该从几分起）")
    if shops:
        top_buckets = collections.defaultdict(lambda: [0, 0])
        for snap in all_snaps:
            items = sorted(snap["items"].values(), key=lambda i: -i.score)
            if not items:
                continue
            bs = by_key.get(snap["key"], [])
            got = any(b["in_snapshot"] for b in bs)
            top_buckets[bucket_of(items[0].score)][1] += 1
            if got:
                top_buckets[bucket_of(items[0].score)][0] += 1
        print("  %-8s %6s %6s %8s" % ("店内最高分", "买了", "店数", "买入率"))
        for lo, hi in BUCKETS:
            key = "%2d-%2d" % (lo, hi)
            got, tot = top_buckets.get(key, [0, 0])
            if tot:
                print("  %-8s %6d %6d %7.1f%%   %s" % (
                    key, got, tot, 100.0 * got / tot,
                    "#" * int(round(100.0 * got / tot / 2.5))))

    print()
    print("=" * 78)
    print("【四】「不买」到底因为什么？")
    if shops:
        n = len(shops)
        for label, pred in (
                ("金币<3（买不起随从）", lambda r: (r["gold"] or 0) < 3),
                ("买了随从", lambda r: r["bought"]),
                ("买了法术", lambda r: r["spell"]),
                ("买了任意牌", lambda r: r["any_buy"]),
                ("场面已满(>=7)", lambda r: (r["board"] or 0) >= 7),
                ("场面已满 且 没买随从",
                 lambda r: (r["board"] or 0) >= 7 and not r["bought"])):
            sub = [r for r in shops if pred(r)]
            if sub:
                print("  %-20s %3d 个（%3.0f%%）  店内最高分 p50=%.1f" % (
                    label, len(sub), 100.0 * len(sub) / n,
                    _q([r["top"] for r in sub], .5)))

    scores = [it.score for it in all_items]
    if scores:
        print()
        print("  商店随从分数： p10=%.1f p25=%.1f p50=%.1f p75=%.1f p90=%.1f max=%.1f" % (
            _q(scores, .1), _q(scores, .25), _q(scores, .5), _q(scores, .75),
            _q(scores, .9), max(scores)))
    if in_snap:
        bs = [b["item"].score for b in in_snap]
        print("  真人买走的分数：p10=%.1f p25=%.1f p50=%.1f p75=%.1f p90=%.1f max=%.1f" % (
            _q(bs, .1), _q(bs, .25), _q(bs, .5), _q(bs, .75), _q(bs, .9), max(bs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
