#!/usr/bin/env python3
"""从 HSReplay 官方 JSON API 拉「饰品梯队 + 英雄选取率」实证数据。

用法：
    /usr/bin/python3 tools/fetch_hsreplay.py                  # 拉两个分段，写 data/trinkets_hsreplay.json
    /usr/bin/python3 tools/fetch_hsreplay.py --print-tier s   # 只打印 S 级饰品
    /usr/bin/python3 tools/fetch_hsreplay.py --bracket ALL    # 只拉一个分段
    /usr/bin/python3 tools/fetch_hsreplay.py --heroes         # 拉英雄选取率，写 data/heroes_hsreplay.json
    /usr/bin/python3 tools/fetch_hsreplay.py --print-heroes   # 不写盘，只打印选取率前 20

为什么要有这个工具：
  攻略文章给的饰品梯队是「作者手感」，会随补丁漂移；而 `rules.py` 要按饰品推荐，
  必须有一个可复现、带时间戳的实证来源。HSReplay 这个接口是**当前唯一**公开暴露
  战棋饰品「选取率 + 平均名次 + 分级」的端点，且无需 API key。

接口（2026-10 实测可用）：
    https://hsreplay.net/api/v1/battlegrounds/trinkets/?BattlegroundsMMRPercentile=<ALL|TOP_1_PERCENT>
  返回 255 条左右，字段：
    trinket_dbf_id / group(lesser|greater) / tier(s|a|b|c|d|f)
    pick_rate / avg_final_placement / final_placement_distribution[8] / extra_data
  ⚠️ 只有极小部分条目带 extra_data（大厅种族上下文），同一 dbfId 会因此出现多行，
     本脚本**保留多行**，不做合并 —— 合并会抹掉「这张牌在特定种族大厅里更强」的信息。

英雄接口（2026-10 实测，`--heroes` 用）：
    https://hsreplay.net/api/v1/battlegrounds/heroes/free/?BattlegroundsMMRPercentile=<ALL|TOP_50_PERCENT|TOP_1_PERCENT>&BattlegroundsTimeRange=LAST_7_DAYS
  返回 92–116 条，字段只有 3 个：
    hero_dbf_id / pick_rate（被 offer 时被选走的百分比）/ armor（起始护甲）
  ⚠️ 路径里的 `/free/` 是必须的：完整版 `/heroes/` 需要登录（403 `Authentication
     credentials were not provided`），未订阅的网页端请求的就是这个 free 端点。
     平均名次与 tier（`adjusted_avg_final_placement` / `tier_v2`）是 Tier7 订阅内容，
     公开渠道拿不到 —— `heroes.py` 因此按「选取率百分位」打分（见该模块头注）。
  ⚠️ 时间窗只认 LAST_7_DAYS（PATCH / LAST_30_DAYS 实测 400）。

⚠️ 需要 User-Agent：不带 UA 的请求会被 HSReplay 403（urllib 默认 UA 会被拒）。

数据来源：
    data/cards_raw_zhCN.json  —— HearthstoneJSON 全量原始下载（10 MB，先跑 tools/build_cards.py）
                                 只有它有 dbfId → CardID 的映射，构建后的 data/cards_zhCN.json 里没有 dbfId。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")

API = "https://hsreplay.net/api/v1/battlegrounds/trinkets/"
BRACKETS = ("ALL", "TOP_1_PERCENT")
DEFAULT_OUT = os.path.join(DATA, "trinkets_hsreplay.json")

#: 英雄端点与饰品不同：路径带 `/free/`，且必须同时给时间窗（见头注）。
HERO_API = "https://hsreplay.net/api/v1/battlegrounds/heroes/free/"
HERO_BRACKETS = ("ALL", "TOP_50_PERCENT", "TOP_1_PERCENT")
HERO_TIME_RANGE = "LAST_7_DAYS"
HERO_DEFAULT_OUT = os.path.join(DATA, "heroes_hsreplay.json")

#: `--bracket` 的合法取值 = 两边分段的并集。
ALL_BRACKETS = ("ALL", "TOP_50_PERCENT", "TOP_1_PERCENT")

# HSReplay 的 tier 排序（注意 's' 在字母序里排在最后，直接 sorted() 会把 S 级沉底）
TIER_ORDER = {"s": 0, "a": 1, "b": 2, "c": 3, "d": 4, "f": 5}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _fetch_json(url, timeout=30, retries=3):
    """GET 一个端点并解析 JSON（重试 + 定制 UA）。失败抛 SystemExit。"""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise SystemExit("拉取失败 %s: %s" % (url, last))


def fetch(bracket, timeout=30, retries=3):
    """拉一个分段的饰品统计。返回原始 list[dict]。"""
    url = "%s?BattlegroundsMMRPercentile=%s" % (API, bracket)
    return _fetch_json(url, timeout=timeout, retries=retries)


def fetch_heroes(bracket, timeout=30, retries=3):
    """拉一个分段的英雄统计。返回原始 list[dict]。

    ⚠️ 必须同时带 `BattlegroundsTimeRange`：不带时端点能返回但数值口径不明，
    网页端固定用 LAST_7_DAYS，跟随它才是可对比的口径。
    """
    url = "%s?BattlegroundsMMRPercentile=%s&BattlegroundsTimeRange=%s" % (
        HERO_API, bracket, HERO_TIME_RANGE)
    return _fetch_json(url, timeout=timeout, retries=retries)


def load_dbf_to_card(path=None):
    """dbfId -> (CardID, 中文名)。只有原始全量表带 dbfId。"""
    path = path or os.path.join(DATA, "cards_raw_zhCN.json")
    if not os.path.exists(path):
        print("⚠️  缺少 %s —— 先跑 tools/build_cards.py 下载全量卡表；本次不映射名字。" % path,
              file=sys.stderr)
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {c["dbfId"]: (c.get("id"), c.get("name")) for c in raw if "dbfId" in c}


def collect(brackets=BRACKETS):
    mapping = load_dbf_to_card()
    out = {
        "source": API,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "HSReplay 公开端点，只暴露选取率与平均名次，不暴露胜率；只统计终局场面。",
        "brackets": {},
    }
    for bracket in brackets:
        rows = fetch(bracket)
        recs = []
        for r in rows:
            card_id, name = mapping.get(r["trinket_dbf_id"], (None, None))
            recs.append({
                "dbf_id": r["trinket_dbf_id"],
                "card_id": card_id,
                "name": name,
                "group": r.get("group"),
                "tier": r.get("tier"),
                "pick_rate": r.get("pick_rate"),
                "avg_final_placement": r.get("avg_final_placement"),
                "placement_dist": r.get("final_placement_distribution"),
                "extra_data": r.get("extra_data"),
            })
        out["brackets"][bracket] = recs
        print("  %-16s %d 条" % (bracket, len(recs)), file=sys.stderr)
    return out


def print_tier(payload, bracket, tier, group=None, top=None):
    rows = [r for r in payload["brackets"].get(bracket, []) if r["tier"] == tier]
    if group:
        rows = [r for r in rows if r["group"] == group]
    rows.sort(key=lambda r: r["avg_final_placement"])
    if top:
        rows = rows[:top]
    print("=== %s / %s 级%s（%d 条，按平均名次升序）" % (
        bracket, tier.upper(), "/" + group if group else "", len(rows)))
    for r in rows:
        print("  %-24s pick %5.1f%%  avg #%.3f  %s" % (
            r["name"] or ("dbf%d" % r["dbf_id"]), r["pick_rate"],
            r["avg_final_placement"], r["card_id"] or ""))


def collect_heroes(brackets=HERO_BRACKETS):
    """拉英雄统计并映射 dbfId → CardID / 中文名。返回落盘用的 payload。"""
    mapping = load_dbf_to_card()
    out = {
        "source": HERO_API,
        "time_range": HERO_TIME_RANGE,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": ("HSReplay 公开端点（/heroes/free/）：免费层只有选取率与护甲，"
                 "没有平均名次/tier（Tier7 订阅内容）。"),
        "brackets": {},
    }
    for bracket in brackets:
        rows = fetch_heroes(bracket)
        recs = []
        for r in rows:
            card_id, name = mapping.get(r["hero_dbf_id"], (None, None))
            recs.append({
                "dbf_id": r["hero_dbf_id"],
                "card_id": card_id,
                "name": name,
                "pick_rate": r.get("pick_rate"),
                "armor": r.get("armor"),
            })
        out["brackets"][bracket] = recs
        print("  %-16s %d 条" % (bracket, len(recs)), file=sys.stderr)
    return out


def print_heroes(payload, bracket, top=None):
    rows = sorted(payload["brackets"].get(bracket, []),
                  key=lambda r: -(r.get("pick_rate") or 0.0))
    if top:
        rows = rows[:top]
    print("=== %s 英雄选取率（%d 条，降序）" % (bracket, len(rows)))
    for r in rows:
        pick = r.get("pick_rate")
        print("  %-24s pick %5.1f%%  armor %2s  %s" % (
            r.get("name") or ("dbf%s" % r.get("dbf_id")),
            pick if pick is not None else 0.0,
            r.get("armor") if r.get("armor") is not None else "?",
            r.get("card_id") or ""))


def _write(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("已写入 %s（%d 字节）" % (path, os.path.getsize(path)), file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="拉取 HSReplay 实证数据（饰品梯队 / 英雄选取率）")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT,
                    help="输出路径（--heroes 时的默认值是 data/heroes_hsreplay.json）")
    ap.add_argument("--bracket", choices=ALL_BRACKETS, action="append",
                    help="只拉指定分段，可重复；饰品默认 ALL+TOP_1_PERCENT，"
                         "英雄默认 ALL+TOP_50_PERCENT+TOP_1_PERCENT")
    ap.add_argument("--print-tier", help="不写盘，只打印指定级别的饰品（s/a/b/c/d/f）")
    ap.add_argument("--group", choices=("lesser", "greater"))
    ap.add_argument("--top", type=int)
    ap.add_argument("--heroes", action="store_true",
                    help="拉英雄选取率，写 data/heroes_hsreplay.json")
    ap.add_argument("--print-heroes", type=int, nargs="?", const=20, metavar="N",
                    help="不写盘，只打印英雄选取率前 N（默认 20）")
    args = ap.parse_args(argv)

    if args.heroes or args.print_heroes is not None:
        brackets = tuple(args.bracket) if args.bracket else HERO_BRACKETS
        payload = collect_heroes(brackets)
        if args.print_heroes is not None:
            for b in brackets:
                print_heroes(payload, b, args.print_heroes)
            return 0
        out = args.out if args.out != DEFAULT_OUT else HERO_DEFAULT_OUT
        _write(out, payload)
        return 0

    brackets = tuple(args.bracket) if args.bracket else BRACKETS
    payload = collect(brackets)

    if args.print_tier:
        for b in brackets:
            print_tier(payload, b, args.print_tier.lower(), args.group, args.top)
        return 0

    _write(args.out, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
