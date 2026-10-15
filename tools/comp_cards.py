#!/usr/bin/env python3
"""把「赛季打法」文档里出现的社区俗称 / 简称映射到真实 CardID，并核对是否出现在本季实测牌池里。

用法：
    /usr/bin/python3 tools/comp_cards.py            # 打印 Markdown 卡表
    /usr/bin/python3 tools/comp_cards.py --check    # 只打印「卡表里查不到」的条目

为什么要有这个工具：攻略文章用的是社区俗称（「摩托猪」「仙女龙」「宰大哥」），
俗称和游戏内卡名常常对不上；直接抄进文档会误导用户，也让后续按 CardID 写规则时无从下手。
这里把每条俗称显式绑定到一个 CardID，跑一遍就能发现绑定错了或卡已经不在本季。

数据来源：
  data/cards_zhCN.json    —— 卡表（HearthstoneJSON zhCN 镜像构建）
  data/pool_current.json  —— 本季实测牌池（tools/dump_pool.py 从真机日志提取）
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (梯队, 种族, 打法名, [(社区俗称, CardID), ...])
COMPS = [
    ("T0", "野兽", "龙虾流", [
        ("龙虾", "BG36_202"),
        ("瑞文", "BG25_354"),
        ("陆行鸟", "BG36_208"),
        ("猎头狮鹫", "BG36_204"),
        ("土狼", "BG36_210"),
        ("鱼饵", "BG36_205"),
        ("鲨鱼", "BG36_206"),
        ("狮子鱼", "BG36_201"),
    ]),
    ("T0", "海盗", "电表倒转", [
        ("上锁宝箱", "BG36_520t"),
        ("逃兵", "BG36_523"),
        ("钩牙", "BG36_344"),
        ("罗杰斯", "BG33_823"),
        ("私掠者", "BG33_825"),
    ]),
    ("T0.5", "恶魔", "吞噬流", [
        ("恶魔干扰者", "BG36_762"),
        ("鬼影幻术师", "BG36_731"),
        ("理性癫狂", "BG36_880"),
        ("巴琳达", "BG35_883"),
    ]),
    ("T0.5", "野猪人", "摩托猪", [
        ("摩托猪", "BG31_323"),
        ("囚牢恶霸", "BG36_333"),
        ("宝石特训", "BG31_893"),
    ]),
    ("T1", "亡灵", "宰大哥", [
        ("时尚魅影", "BG36_515"),
        ("变装盗墓贼", "BG28_303"),
        ("宰割", "BG28_604"),
    ]),
    ("T1", "机械", "火花破坏机", [
        ("火花破坏机", "BG36_851"),
        ("复映无人机", "BG36_506"),
    ]),
    ("T1.5", "元素", "雷暴", [
        ("无羁雷暴", "BG36_352"),
        ("空气亡魂", "BG34_858"),
        ("诺米", "BG30_MagicItem_544"),
        ("热舞旋风", "BG26_162"),
        ("冰灵", "BG26_537"),
    ]),
    ("T1.5", "龙", "守备巨龙", [
        ("威猛龙息", "BG36_246"),
        ("守备巨龙", "BG36_241"),
        ("青铜时光行者", "BG36_242"),
        ("诗心龙", "BG29_813"),
        ("仙女龙", "BG24_004"),
    ]),
    ("T2", "鱼人", "手牌鱼", [
        ("海藻卫士", "BG36_701"),
        ("铜须", "BG_LOE_077"),
        ("茉艾拉·铜须", "BG27_518"),
        ("逃生飞龙", "BG36_243"),
        ("怒胆鱼人", "BG33_318"),
        ("萨满招潮者", "BG36_704"),
        ("发现鱼", "BG22_202"),
    ]),
    ("T2", "纳迦", "涌流毁灭者", [
        ("涌流毁灭者", "BG36_622"),
        ("巴琳达", "BG35_883"),
        ("仙女龙", "BG24_004"),
    ]),
]


def load_cards():
    with open(os.path.join(HERE, "data", "cards_zhCN.json"), encoding="utf-8") as fp:
        blob = json.load(fp)
    cards = blob["cards"] if isinstance(blob, dict) and "cards" in blob else blob
    seq = list(cards.values()) if isinstance(cards, dict) else cards
    return {c["id"]: c for c in seq if c.get("id")}


def load_pool():
    path = os.path.join(HERE, "data", "pool_current.json")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fp:
        blob = json.load(fp)
    shop = blob.get("shop") or []
    return {c["card_id"] for c in shop if c.get("card_id")}


def strip(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("|", "/").replace("\n", " ")


def main(argv):
    check_only = "--check" in argv
    cards = load_cards()
    pool = load_pool()
    missing = []
    rows = []
    for tier, tribe, comp, entries in COMPS:
        for alias, cid in entries:
            card = cards.get(cid)
            if card is None:
                missing.append((tier, tribe, comp, alias, cid, "卡表中没有这个 CardID"))
                continue
            if card.get("type") == "BATTLEGROUND_TRINKET":
                kind = "饰品"
            elif card.get("type") == "BATTLEGROUND_SPELL":
                kind = "法术"
            else:
                kind = "随从"
            rows.append((
                tier, tribe, comp, alias, card.get("name") or "",
                cid, card.get("techLevel"), kind,
                ",".join(card.get("races") or []) or "无",
                "是" if cid in pool else "—",
            ))
    if check_only:
        for item in missing:
            print("查不到: %s" % " / ".join(str(x) for x in item))
        print("\n共 %d 条绑定，%d 条查不到" % (len(rows) + len(missing), len(missing)))
        return 1 if missing else 0

    print("| 梯队 | 种族 | 打法 | 文档俗称 | 游戏内卡名 | CardID | 星级 | 类型 | 卡牌种族 | 本季实测 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print("| %s | %s | %s | %s | %s | `%s` | %s | %s | %s | %s |" % (
            r[0], r[1], r[2], r[3], r[4], r[5], r[6] if r[6] is not None else "—",
            r[7], r[8], r[9]))
    if missing:
        print("\n**查不到的绑定**（文档里不要写死，需人工确认）：")
        for item in missing:
            print("- %s" % " / ".join(str(x) for x in item))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
