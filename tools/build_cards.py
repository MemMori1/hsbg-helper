#!/usr/bin/env python3
"""构建战棋卡牌缓存。

把 HearthstoneJSON 的 zhCN 全量卡表（10.1 MB / 36,390 张）裁成只含
`set == "BATTLEGROUNDS"` 的紧凑缓存（`data/cards_zhCN.json`，约 300 KB）。

用法：
    ./build-cards                 # 联网下载并重建缓存
    ./build-cards --verify PATH   # 只校验现有缓存，不联网
    ./build-cards --input FILE    # 用已下载好的全量 JSON 重建（离线）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from hsbg import cards as C  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建 hsbg-helper 的战棋卡牌缓存")
    ap.add_argument("--output", default=str(C.DEFAULT_CARD_DB_PATH),
                    help="缓存输出路径")
    ap.add_argument("--input", default=None,
                    help="已下载的 HearthstoneJSON 全量 JSON（离线构建）")
    ap.add_argument("--url", default=C.CARD_DB_URL, help="下载地址")
    # 英文卡表只服务 JevK5 的英文题面（ADR-4），单独构建。
    ap.add_argument("--lang", choices=("zhCN", "enUS"), default="zhCN",
                    help="语言：zhCN（展示用）或 enUS（只喂模型）")
    args = ap.parse_args(argv)

    if args.lang == "enUS":
        # 英文卡表：输出路径与 URL 都换成 enUS 那套
        if args.output == str(C.DEFAULT_CARD_DB_PATH):
            args.output = str(C.DEFAULT_CARD_DB_EN_PATH)
        if args.url == C.CARD_DB_URL:
            args.url = C.CARD_DB_EN_URL

    out = Path(args.output)

    if args.input:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        print(f"读出全量卡表 {len(raw):,} 张（{args.input}）")
    else:
        print(f"下载 {args.url} …")
        raw = C.download_cards_json(args.url)
        print(f"下载完成，全量 {len(raw):,} 张")

    cache = C.build_cache(raw)
    C.save_cache(cache, out)
    size_kb = out.stat().st_size / 1024
    print(f"已写入 {out}（{cache['count']:,} 张战棋卡，{size_kb:.0f} KB）")

    db = C.CardDB.load(out)
    st = db.stats()
    print(f"  随从 {st['minions']:,} 张，其中当期随从池 {st['pool_minions']:,} 张")
    print(f"  种族 {st['races']} 种：{', '.join(db.races())}")
    print(f"  星级：{db.tech_levels()}")
    for tl in db.tech_levels():
        print(f"    {tl} 星：{len(db.by_tech_level(tl))} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
