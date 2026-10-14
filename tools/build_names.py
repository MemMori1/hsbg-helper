#!/usr/bin/env python3
"""从真实日志自举 `CardID → 中文名` 表，并用卡表交叉校验。

用法：
    ./tools/build_names.py                      # 扫最近一份 Power.log
    ./tools/build_names.py --all                # 扫全部会话
    ./tools/build_names.py --log PATH --out PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from hsbg import names as N          # noqa: E402
from hsbg.cards import CardDB, DEFAULT_CARD_DB_PATH  # noqa: E402
from hsbg.config import DEFAULT_LOG_ROOT  # noqa: E402
from hsbg.logs import list_sessions  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从日志自举 CardID→中文名")
    ap.add_argument("--log", action="append", default=None, help="指定 Power.log（可多次）")
    ap.add_argument("--all", action="store_true", help="扫描全部会话的 Power.log")
    ap.add_argument("--out", default=str(N.DEFAULT_NAME_DB_PATH), help="输出路径")
    ap.add_argument("--db", default=str(DEFAULT_CARD_DB_PATH), help="卡表路径（用于校验）")
    args = ap.parse_args(argv)

    logs = []
    if args.log:
        logs = [Path(p) for p in args.log]
    else:
        sessions = list_sessions(DEFAULT_LOG_ROOT)
        picked = sessions if args.all else sessions[:1]
        logs = [s.power_log for s in picked if s.has_power_log]

    if not logs:
        print("没找到 Power.log", file=sys.stderr)
        return 1

    from hsbg.parser import iter_file

    all_names = {}
    for p in logs:
        found = N.collect_names_from_events(iter_file(p))
        print(f"  {p.parent.name}/Power.log → {len(found)} 条")
        for k, v in found.items():
            all_names.setdefault(k, v)

    out = N.save_names(all_names, Path(args.out), source=", ".join(str(p) for p in logs))
    print(f"已写入 {out}（{len(all_names)} 条）")

    db = CardDB.load_or_empty(Path(args.db))
    rep = db.verify_against_log_names(all_names)
    print(f"卡表交叉校验：{rep['checked']} 条 → 一致 {rep['ok']} / "
          f"不一致 {rep['mismatch']} / 卡表缺失 {rep['missing']}")
    for s in rep["samples"]:
        print(f"   差异 {s['card_id']}: 日志 {s['log']!r} vs 卡表 {s['db']!r}")
    for m in rep["missing_samples"]:
        print(f"   缺失 {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
