#!/usr/bin/env python3
"""解析器分支探针：实测每一类日志行到底走进哪个分支。

背景：P0 收尾时发现「实体属性行 vs 块内裸 tag 行」的分派口径反复摇摆
（吸收数在 3,919 与 118,084 之间跳），每次改都是猜。这个探针不猜，
直接把 `_TAG_LINE_RE` / `_INFO_TAG_LINE_RE` 对真实载荷的命中数打出来，
口径就用数据定，然后用 tests/baseline.json 锁住。

用法::

    cd hsbg-helper && PYTHONPATH=src /usr/bin/python3 tools/probe_tag_dispatch.py
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg.config import DEFAULT_LOG_ROOT  # noqa: E402
from hsbg.logs import latest_power_log  # noqa: E402
from hsbg.parser import (  # noqa: E402
    _CMD_RE,
    _INFO_TAG_LINE_RE,
    _LINE_RE,
    _TAG_LINE_RE,
)

#: 日志里 `dash 之后` 的真实空格数只有这几档（实测 230,038 行 tag 行的分布）
SPACE_AFTER_DASH = collections.Counter()
DISPATCH = collections.Counter()
INDENT_HIST = collections.Counter()


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_power_log(DEFAULT_LOG_ROOT)
    print(f"探针目标 {path}\n")

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            m = _LINE_RE.match(line)
            if not m:
                continue
            if m.group("source") != "GameState.DebugPrintPower":
                continue
            raw = m.group("payload")
            stripped = raw.strip()

            # dash 之后的真实空格数
            idx = line.find("() - ")
            if idx >= 0:
                after = line[idx + len("() - ") :]
                SPACE_AFTER_DASH[len(after) - len(after.lstrip(" "))] += 1

            if _TAG_LINE_RE.match(raw):
                DISPATCH["TAG_LINE_RE(raw 未 strip)"] += 1
            if _TAG_LINE_RE.match(stripped):
                DISPATCH["TAG_LINE_RE(strip 后)"] += 1
            if _INFO_TAG_LINE_RE.match(stripped):
                DISPATCH["INFO_TAG_LINE_RE(strip 后)"] += 1
            if _CMD_RE.match(stripped):
                DISPATCH[f"CMD={_CMD_RE.match(stripped).group('cmd')}"] += 1

            if re.match(r"^\s*tag=", raw):
                INDENT_HIST[len(raw) - len(raw.lstrip())] += 1

    print("dash 之后空格数分布（它决定载荷缩进 = 该值 - 1）:")
    for k, v in sorted(SPACE_AFTER_DASH.items()):
        print(f"  {k:>3} 空格  {v:>9,} 行")

    print("\n载荷缩进分布（只统计 tag= 行）:")
    for k, v in sorted(INDENT_HIST.items()):
        print(f"  indent={k:<3} {v:>9,} 行")

    print("\n各正则 / 命令命中数:")
    for k, v in sorted(DISPATCH.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<34} {v:>9,}")

    print("\n口径结论:")
    raw_hits = DISPATCH["TAG_LINE_RE(raw 未 strip)"]
    strip_hits = DISPATCH["TAG_LINE_RE(strip 后)"]
    info_hits = DISPATCH["INFO_TAG_LINE_RE(strip 后)"]
    print(f"  未 strip 时能认出 {raw_hits:,} 行 tag 行")
    print(f"  strip 之后只能认出 {strip_hits:,} 行 → 说明 strip 会毁掉缩进信号")
    print(f"  顶格形态（INFO_TAG_LINE_RE）{info_hits:,} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
