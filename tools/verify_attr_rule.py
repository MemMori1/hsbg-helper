#!/usr/bin/env python3
"""验证「属性行缩进 = 宿主缩进 + 4」这条归属规则在真机上是否站得住。

判据（不是数数，是语义自证）：
  1. 被吸收的属性行里，是否普遍带 `tag=ENTITY_ID`，且值等于宿主 id？
     如果规则错了，吸收进来的会是别的块的行，ENTITY_ID 对不上。
  2. 各类宿主命令（FULL_ENTITY / SHOW_ENTITY / BLOCK_START / Player）后面
     紧跟的 tag 行缩进是否确实都是 宿主缩进 + 4。
  3. 落在 BLOCK_TAG（未被吸收）的 tag 行，宿主是谁、为什么不满足 +4。

用法::

    cd hsbg-helper && PYTHONPATH=src /usr/bin/python3 tools/verify_attr_rule.py
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg.config import DEFAULT_LOG_ROOT  # noqa: E402
from hsbg.events import EventType  # noqa: E402
from hsbg.logs import latest_power_log  # noqa: E402
from hsbg.parser import PowerLogParser  # noqa: E402

_HEAD_CMDS = {"FULL_ENTITY", "SHOW_ENTITY", "CHANGE_ENTITY", "BLOCK_START", "PLAYER"}


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_power_log(DEFAULT_LOG_ROOT)
    print(f"验证目标 {path}\n")

    parser = PowerLogParser()
    #: 宿主命令 -> 收到的属性行数
    host_attr = collections.Counter()
    #: 宿主命令 -> {属性行缩进: 次数}
    host_indent = collections.defaultdict(collections.Counter)
    #: 吸收进来的属性行里 ENTITY_ID 与宿主一致 / 不一致 / 没有
    id_match = collections.Counter()
    #: BLOCK_TAG 的 (宿主命令, 缩进) 分布
    block_tag_ctx = collections.Counter()
    #: 深度归属自证：ATTACHED_TAG 的 owner 是否 = 缩进-4 深度上登记的实体
    depth_ok = collections.Counter()

    last_host_cmd = ""
    last_host_indent = -1
    last_host_id = None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            events = parser.feed_line(raw_line)
            for ev in events:
                if ev.kind == EventType.BLOCK_TAG:
                    block_tag_ctx[(ev.parent_cmd or "?", ev.indent)] += 1
                    continue
                if ev.kind == EventType.ATTACHED_TAG:
                    expected = parser._entity_by_indent.get(ev.indent - 4)
                    depth_ok[
                        "owner == 深度表（缩进-4）✓" if expected == ev.owner_id
                        else "owner 与深度表不一致 ✗"
                    ] += 1
                    continue
                # 头命令：记下宿主
                if ev.kind in (
                    EventType.FULL_ENTITY,
                    EventType.SHOW_ENTITY,
                    EventType.CHANGE_ENTITY,
                ) and ev.tags:
                    host_attr[ev.kind] += 1
                    host_indent[ev.kind][ev.indent] += 1
                    eid = ev.tags.get("ENTITY_ID")
                    if eid is None:
                        id_match["无 ENTITY_ID 属性"] += 1
                    elif ev.entity_id is not None and int(eid) == ev.entity_id:
                        id_match["ENTITY_ID 与宿主一致 ✓"] += 1
                    else:
                        id_match["ENTITY_ID 与宿主不一致 ✗"] += 1

        parser.flush()

    print("① 带属性行的实体（按命令类型）:")
    for k, v in host_attr.most_common():
        indents = dict(host_indent[k])
        print(f"  {k:<16} {v:>6,} 个   宿主缩进分布={indents}")

    print("\n② 属性行 ENTITY_ID 自证（规则正确性的硬判据）:")
    for k, v in id_match.most_common():
        print(f"  {k:<28} {v:>7,}")

    print("\n③ 深度归属（ATTACHED_TAG 的 owner 必须 = 缩进-4 处最近声明的实体）:")
    for k, v in depth_ok.most_common():
        print(f"  {k:<28} {v:>7,}")

    print("\n④ 未被吸收的 tag 行（BLOCK_TAG）宿主与缩进 Top15:")
    for (cmd, ind), v in block_tag_ctx.most_common(15):
        print(f"  宿主={cmd:<14} 缩进={ind:<3} {v:>7,}")
    print(f"  BLOCK_TAG 合计 {sum(block_tag_ctx.values()):,}")
    print(f"  连深度表都定不出归属的 {parser.stats.unowned_tags:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
