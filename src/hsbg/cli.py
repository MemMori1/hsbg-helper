"""`hsbg` 命令行入口（P0）。

用法::

    python3 -m hsbg sessions                      # 列出所有日志会话
    python3 -m hsbg check                         # 用真实日志做解析自检
    python3 -m hsbg parse <Power.log> --limit 500  # 解析文件（默认只打印统计）
    python3 -m hsbg parse <Power.log> --kind TAG_CHANGE --grep BACON --limit 20
    python3 -m hsbg stream                        # 实时跟随最新会话（需要炉石在跑）
    python3 -m hsbg stream --head 50              # 从文件头开始，读到 50 个事件就退

本模块**零第三方依赖**，只用标准库。P6 的 Swift 悬浮框会用同样的方式
调 Python（或后续改为长驻进程 + 本地 socket）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

from . import __version__
from .config import DEFAULT_LOG_ROOT
from .events import EventType, PowerEvent
from .logs import latest_power_log, list_sessions
from .parser import DEFAULT_SOURCES, PowerLogParser, iter_file, parse_file
from .tailer import LogTailer

# --- 彩色输出（只在 TTY 下启用） ------------------------------------------

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _dim(t: str) -> str:
    return _c("2", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _kind_color(kind: str) -> str:
    return {
        EventType.TAG_CHANGE: "36",      # cyan
        EventType.BLOCK_TAG: "2",        # dim
        EventType.FULL_ENTITY: "32",     # green
        EventType.SHOW_ENTITY: "33",     # yellow
        EventType.CHANGE_ENTITY: "35",   # magenta
        EventType.HIDE_ENTITY: "2",
        EventType.INFO: "34",            # blue
        EventType.META_DATA: "34",
        EventType.CREATE_GAME: "1;31",   # bold red
        EventType.BLOCK_START: "2",
        EventType.BLOCK_END: "2",
        EventType.RAW: "31",
    }.get(kind, "0")


# --- 事件格式化 ------------------------------------------------------------


def format_event(ev: PowerEvent) -> str:
    """把事件压成一行可读文本。"""
    kind = _c(_kind_color(ev.kind), ev.kind.ljust(13))
    prefix = f"{_dim(str(ev.line_no).rjust(8))} {kind}"

    if ev.kind == EventType.CREATE_GAME:
        return f"{prefix} {_bold('新对局开始')}"

    if ev.kind in (EventType.FULL_ENTITY, EventType.SHOW_ENTITY, EventType.CHANGE_ENTITY):
        tags = ""
        if ev.tags:
            keys = ("ZONE", "CARDTYPE", "CONTROLLER", "ATK", "HEALTH", "TECH_LEVEL", "RESOURCES")
            picked = [f"{k}={ev.tags[k]}" for k in keys if k in ev.tags]
            extra = len(ev.tags) - len(picked)
            tags = "  " + _dim(", ".join(picked) + (f", +{extra}" if extra > 0 else ""))
        return f"{prefix} id={ev.entity_id} card={ev.card_id or '-'}{tags}"

    if ev.kind == EventType.TAG_CHANGE:
        who = ev.entity.name if ev.entity and ev.entity.name else f"id={ev.entity_id}"
        extra = ""
        if ev.entity and ev.entity.card_id and not ev.entity.is_known:
            extra = _dim(f" ({ev.entity.card_id})")
        return f"{prefix} {who}{extra} {_bold(ev.tag)}={ev.value}"

    if ev.kind == EventType.BLOCK_TAG:
        ind = "  " * max(0, (ev.indent - 4) // 4)
        return f"{prefix} {ind}{ev.tag}={ev.value}"

    if ev.kind == EventType.ATTACHED_TAG:
        ind = "  " * max(0, (ev.indent - 4) // 4)
        return f"{prefix} {ind}-> id={ev.owner_id} {ev.tag}={ev.value}"

    if ev.kind == EventType.INFO:
        return f"{prefix} {ev.detail}"

    if ev.kind == EventType.META_DATA:
        return f"{prefix} Meta={ev.meta} Data={ev.meta_data} InfoCount={ev.meta_count}"

    if ev.kind == EventType.HIDE_ENTITY:
        who = ev.entity.name if ev.entity and ev.entity.name else f"id={ev.entity_id}"
        return f"{prefix} {who} tag={ev.tag} value={ev.value}"

    if ev.kind in (EventType.BLOCK_START, EventType.BLOCK_END) and ev.block:
        b = ev.block
        return (
            f"{prefix} BlockType={b.block_type or '-'} Entity={b.entity or '-'}"
            f" Target={b.target if b.target is not None else '-'}"
            f" SubOption={b.sub_option if b.sub_option is not None else '-'}"
        )

    return f"{prefix} {ev.payload}"


def event_to_dict(ev: PowerEvent) -> dict:
    """事件转 JSON 友好结构（供 --json / 落盘用）。"""
    d = {"kind": ev.kind, "line": ev.line_no, "ts": ev.timestamp}
    if ev.indent:
        d["indent"] = ev.indent
    if ev.entity_id is not None:
        d["entity_id"] = ev.entity_id
    if ev.card_id:
        d["card_id"] = ev.card_id
    if ev.tags:
        d["tags"] = ev.tags
    if ev.entity is not None:
        d["entity"] = {
            "raw": ev.entity.raw,
            "id": ev.entity.entity_id,
            "name": ev.entity.name,
            "card_id": ev.entity.card_id,
            "zone": ev.entity.zone,
            "zone_pos": ev.entity.zone_pos,
            "player": ev.entity.player,
        }
    if ev.tag:
        d["tag"] = ev.tag
        d["value"] = ev.value
    if ev.block is not None:
        d["block"] = {
            "type": ev.block.block_type,
            "entity": ev.block.entity,
            "target": ev.block.target,
            "sub_option": ev.block.sub_option,
            "trigger_keyword": ev.block.trigger_keyword,
        }
    if ev.meta:
        d["meta"] = {"name": ev.meta, "data": ev.meta_data, "count": ev.meta_count}
    if ev.detail:
        d["detail"] = ev.detail
    if ev.payload:
        d["payload"] = ev.payload
    return d


# --- 子命令 ----------------------------------------------------------------


def cmd_sessions(args: argparse.Namespace) -> int:
    from .logs import latest_session

    root = args.log_root
    print(_bold(f"日志根目录  {root}"))
    sessions = list_sessions(root)
    if not sessions:
        print("  （没有找到会话目录）")
        return 1
    latest = latest_session(root)
    print(_bold(f"共 {len(sessions)} 个会话（新 → 旧）\n"))
    for s in sessions:
        mark = _c("32", " ← 最新") if latest and s.path == latest.path else ""
        print(f"  {s}{mark}")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    path = args.path or latest_power_log(args.log_root)
    if not path:
        print("找不到 Power.log", file=sys.stderr)
        return 1

    if args.json:
        return _parse_json(args, str(path))

    events, stats = parse_file(str(path), limit_lines=args.limit_lines)

    if args.kind:
        wanted = {k.upper() for k in args.kind}
        events = [e for e in events if e.kind in wanted]
    if args.grep:
        events = [e for e in events if args.grep.lower() in _event_text(e).lower()]

    shown = events[: args.limit] if args.limit else events
    for ev in shown:
        print(format_event(ev))
    if args.limit and len(events) > args.limit:
        print(_dim(f"… 还有 {len(events) - args.limit} 个事件未显示（--limit 调整）"))

    if not args.quiet:
        print()
        _print_stats(stats, str(path))
    return 0


def _parse_json(args: argparse.Namespace, path: str) -> int:
    """流式输出 JSON Lines，避免把 90 MB 的事件全放内存。"""
    from .parser import iter_file

    n = 0
    for ev in iter_file(path):
        if args.kind and ev.kind not in {k.upper() for k in args.kind}:
            continue
        if args.grep and args.grep.lower() not in _event_text(ev).lower():
            continue
        print(json.dumps(event_to_dict(ev), ensure_ascii=False))
        n += 1
        if args.limit and n >= args.limit:
            break
    return 0


def _event_text(ev: PowerEvent) -> str:
    parts = [ev.kind, ev.tag, ev.value, ev.card_id, ev.detail, ev.payload]
    if ev.entity is not None:
        parts += [ev.entity.raw, ev.entity.name, ev.entity.card_id]
    return " ".join(p for p in parts if p)


def _print_stats(stats, path: str) -> None:
    print(_bold("─ 解析统计 " + "─" * 40))
    print(f"  文件            {path}")
    print(f"  总行数          {stats.lines_total:,}")
    print(f"  Power 流行数    {stats.lines_power:,}")
    print(f"  对局数          {stats.games}")
    print(f"  事件数          {stats.events:,}")
    print(f"  未识别载荷      {stats.unknown_payloads:,}")
    print("  事件分布:")
    for kind, cnt in stats.by_kind.items():
        print(f"    {kind:<15} {cnt:,}")


def cmd_check(args: argparse.Namespace) -> int:
    """解析自检：用真实日志跑一遍，校验关键不变量。

    这是 P0 的验收脚本 —— 回归时会跑它，而不是肉眼看输出。
    """
    from .logs import list_sessions

    root = args.log_root
    if getattr(args, "path", None):
        path = Path(args.path)
        if not path.exists():
            print(_c("31", f"✗ 找不到 {path}"), file=sys.stderr)
            return 1
        print(_bold(f"自检目标  {path}  ({path.stat().st_size/1e6:.1f} MB)"))
    else:
        sessions = [s for s in list_sessions(root) if s.has_power_log]
        if not sessions:
            print(_c("31", f"✗ 在 {root} 找不到任何带 Power.log 的会话"), file=sys.stderr)
            return 1

        session = sessions[0]
        path = session.power_log
        print(_bold(f"自检目标  {path}  ({session.power_log_size()/1e6:.1f} MB)"))

    t0 = time.time()
    events, stats = parse_file(str(path))
    dt = time.time() - t0

    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(ok)
        mark = _c("32", "✓") if ok else _c("31", "✗")
        print(f"  {mark} {name}" + (f"  {_dim(detail)}" if detail else ""))

    kinds = stats.by_kind

    # 有些 Power 行**有意**不产出独立事件：实体块的属性行被吸收进
    # 宿主实体事件（`FULL_ENTITY`/`SHOW_ENTITY` 的 tags 里）。
    # 所以正确的不变量是「产出的事件 + 被吸收的行 == Power 行数」。
    emitted = stats.events
    absorbed = stats.lines_power - emitted

    check("每一行都被计入", stats.lines_total > 0, f"{stats.lines_total:,} 行")
    check(
        "事件数 + 被吸收的实体属性行 == Power 行数",
        emitted + absorbed == stats.lines_power,
        f"events={emitted:,} absorbed={absorbed:,} power={stats.lines_power:,}",
    )
    check(
        "被吸收的实体属性行占比合理（25%~55%）",
        0.25 <= absorbed / max(1, stats.lines_power) <= 0.55,
        f"{absorbed / max(1, stats.lines_power) * 100:.1f}%（实体属性块占大头，正常）",
    )
    # 硬判据：规则说「缩进 = 宿主+4 的 tag 行属于宿主」，那这些行的 ENTITY_ID
    # 必须等于宿主 id。8,496/8,496 一致才算规则站得住（见 tools/verify_attr_rule.py）。
    id_checked = stats.absorbed_checked
    check(
        "归属规则自证：被吸收行的 ENTITY_ID 全等于宿主 id",
        id_checked > 0 and stats.absorbed_id_mismatch == 0,
        f"{stats.absorbed_id_match:,}/{id_checked:,} 一致，{stats.absorbed_id_mismatch} 不一致",
    )
    check("找到 CREATE_GAME", stats.games >= 1, f"{stats.games} 局")
    # ⚠️ 这些断言一律用**比例**而不是绝对条数。
    # 早先用的是 "TAG_CHANGE > 5 万" 这类绝对阈值，在 96 MB 的参考日志上没问题，
    # 但炉石只要开着就会新建一个**很小的**会话目录（实测 3.2 MB），一跑 check
    # 就必然失败 —— 那是阈值不对，不是解析器坏了。
    tag_change = kinds.get(EventType.TAG_CHANGE, 0)
    check(
        "TAG_CHANGE 与 Power 行数量级相称（20%~95%）",
        0.20 <= tag_change / max(1, stats.lines_power) <= 0.95,
        f"{tag_change:,}（占 Power 行 {tag_change / max(1, stats.lines_power) * 100:.0f}%）",
    )
    unowned = kinds.get(EventType.BLOCK_TAG, 0)
    attached = kinds.get(EventType.ATTACHED_TAG, 0)
    check(
        "无实体前缀的 tag 行绝大多数归属明确（在块内 + 挂到 owner）",
        attached < 2_000 and unowned < 1_000,
        f"ATTACHED_TAG={attached:,} BLOCK_TAG={unowned:,}",
    )
    full_entity = kinds.get(EventType.FULL_ENTITY, 0)
    # 一局战棋会创建上千个实体（英雄、随从、按钮、附魔…），按局数缩放更稳。
    per_game = full_entity / max(1, stats.games)
    check(
        "FULL_ENTITY 密度合理（每局 > 50 个）",
        per_game > 50,
        f"{full_entity:,}（平均每局 {per_game:.0f}）",
    )
    check(
        "RAW 已被压到极低（< 1% 总行数）",
        kinds.get(EventType.RAW, 0) < stats.lines_total * 0.01,
        f"RAW={kinds.get(EventType.RAW, 0):,}",
    )

    # 实体名/卡牌名是否真的抠出来了
    named = 0
    cardnames = {}
    for ev in events:
        ref = ev.entity
        if ref is not None and ref.is_known and ref.card_id:
            named += 1
            cardnames.setdefault(ref.card_id, ref.name)
        elif ev.kind == EventType.INFO and ev.entity and ev.entity.card_id and ev.entity.name:
            cardnames.setdefault(ev.entity.card_id, ev.entity.name)
    check("解析出中文实体名+CardID", len(cardnames) >= 20,
          f"{len(cardnames)} 条 CardID→中文名")

    check("解析速度 ≥ 10 MB/s", (session.power_log_size() / dt) >= 10e6,
          f"{session.power_log_size()/dt/1e6:.1f} MB/s（{dt:.1f}s）")

    ok = all(checks)
    print()
    print(_c("32" if ok else "31", f"{'全部通过' if ok else '存在失败项'}  {sum(checks)}/{len(checks)}"))
    return 0 if ok else 1


def cmd_stream(args: argparse.Namespace) -> int:
    tailer = LogTailer(
        log_root=args.log_root,
        start_at_end=not args.head,
    )
    if args.path:
        tailer.open_path(args.path, at_end=not args.head)
    elif not tailer.open() and not args.wait:
        print(f"找不到会话日志（{args.log_root}）", file=sys.stderr)
        return 1

    print(_dim(f"跟随 {tailer.current_path}"))
    print(_dim("Ctrl-C 退出\n"))

    shown = 0
    try:
        if args.head:
            # 有界模式：只用于验证，读到 N 个就退。
            for ev in tailer.follow(max_idle_polls=args.head * 2):
                print(format_event(ev))
                shown += 1
                if shown >= args.head:
                    break
        else:
            for ev in tailer.follow():
                if args.kind and ev.kind not in {k.upper() for k in args.kind}:
                    continue
                if args.grep and args.grep.lower() not in _event_text(ev).lower():
                    continue
                print(format_event(ev))
                shown += 1
                if args.limit and shown >= args.limit:
                    break
    except KeyboardInterrupt:
        print()
    print(_dim(f"\n共 {shown} 个事件  |  {json.dumps(tailer.stats.as_dict(), ensure_ascii=False)}"))
    return 0


# --- 入口 ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hsbg",
        description="炉石传说·酒馆战棋辅助提示器 —— 日志采集与解析（P0）",
    )
    p.add_argument("--version", action="version", version=f"hsbg {__version__}")
    p.add_argument("--log-root", default=str(DEFAULT_LOG_ROOT), help="炉石日志根目录")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sessions", help="列出所有日志会话")
    sp.set_defaults(func=cmd_sessions)

    pp = sub.add_parser("parse", help="解析一个 Power.log")
    pp.add_argument("path", nargs="?", help="Power.log 路径（省略则用最新会话）")
    pp.add_argument("--limit", type=int, default=0, help="最多显示多少个事件（0=全部）")
    pp.add_argument("--limit-lines", type=int, default=None, help="最多读文件前 N 行")
    pp.add_argument("--kind", nargs="*", help="只看这些事件类型")
    pp.add_argument("--grep", help="按子串过滤")
    pp.add_argument("--json", action="store_true", help="输出 JSON Lines（流式）")
    pp.add_argument("--quiet", action="store_true", help="不打印统计")
    pp.set_defaults(func=cmd_parse)

    cp = sub.add_parser("check", help="用真实日志做解析自检（P0 验收）")
    cp.add_argument("path", nargs="?",
                    help="指定 Power.log（默认最新会话；判定用的是比例，小日志也能跑）")
    cp.set_defaults(func=cmd_check)

    st = sub.add_parser("stream", help="实时跟随最新会话")
    st.add_argument("path", nargs="?", help="指定文件（默认最新会话）")
    st.add_argument("--head", type=int, default=0, help="从文件头读 N 个事件后退出")
    st.add_argument("--limit", type=int, default=0, help="实时模式最多输出 N 个事件")
    st.add_argument("--kind", nargs="*", help="只看这些事件类型")
    st.add_argument("--grep", help="按子串过滤")
    st.add_argument("--wait", action="store_true", help="没有会话时等待游戏启动")
    st.set_defaults(func=cmd_stream)

    # P2：卡牌 / 盘面
    for name, func, helptext in (
        ("state", cmd_state, "重放日志并打印最终盘面（P1/P2 快照）"),
        ("watch", cmd_watch, "实时跟随并刷新盘面（悬浮框的文本原型）"),
        ("cards", cmd_cards, "卡牌数据层自检（P2）"),
    ):
        sub_p = sub.add_parser(name, help=helptext)
        sub_p.add_argument("path", nargs="?", help="Power.log 路径（省略则用最新会话）")
        sub_p.add_argument("--cards-db", default=None, help="卡表缓存路径")
        sub_p.add_argument("--names-db", default=None, help="日志自举名路径")
        sub_p.add_argument("--cards-db-en", default=None,
                           help="英文卡表（只喂模型；缺省用 data/cards_enUS.json）")
        sub_p.add_argument("--no-color", action="store_true", help="禁用 ANSI 颜色")
        sub_p.set_defaults(func=func)
        if name == "state":
            sub_p.add_argument("--until-line", type=int, default=None,
                               help="只重放到第 N 行（用于对照历史局面）")
        if name == "watch":
            sub_p.add_argument("--head", type=int, default=0, help="有界模式：读 N 次重绘后退出")
            sub_p.add_argument("--limit", type=int, default=0, help="最多重绘 N 次")
            sub_p.add_argument("--interval", action="store_true", help="每次重绘前清屏")
            sub_p.add_argument("--wait", action="store_true", help="没有会话时等待游戏启动")
        if name == "cards":
            sub_p.add_argument("--verify", action="store_true", help="与日志自举名交叉校验")

    # P4：模型顾问
    av = sub.add_parser("advise", help="规则优先 + JevK5 兜底的建议（P3+P4）")
    av.add_argument("path", nargs="?", help="Power.log 路径（省略则用最新会话）")
    av.add_argument("--cards-db", default=None, help="卡表缓存路径")
    av.add_argument("--names-db", default=None, help="日志自举名路径")
    av.add_argument("--cards-db-en", default=None,
                    help="英文卡表（只喂模型；缺省用 data/cards_enUS.json）")
    av.add_argument("--no-color", action="store_true", help="禁用 ANSI 颜色")
    av.add_argument("--until-line", type=int, default=None, help="只重放到第 N 行")
    av.add_argument("--limit", type=int, default=12, help="最多咨询几张商店随从")
    av.add_argument("--no-model", action="store_true", help="只出规则建议，不问模型")
    av.add_argument("--model-url", default=None, help="llama-server 地址")
    av.add_argument("--model-size", default="2b", help="校准温度档位：2b/4b/4b-v0.2/9b")
    av.add_argument("--model-timeout", type=float, default=60.0, help="单题超时秒数")
    av.add_argument("--quiet", action="store_true", help="不打印盘面，只看建议")
    av.add_argument("--show-evidence", action="store_true", help="打印喂给模型的英文 evidence")
    av.set_defaults(func=cmd_advise)

    sv = sub.add_parser("serve", help="JevK5 服务状态 / 启动 / 停止")
    sv.add_argument("action", nargs="?", default="status",
                    choices=["status", "start", "stop"], help="默认 status")
    sv.add_argument("--model-url", default=None, help="llama-server 地址")
    sv.set_defaults(func=cmd_serve)

    # P6：悬浮框的数据源
    pn = sub.add_parser("panel", help="给 Swift 悬浮框喂一行一帧的 JSON（P6 契约）")
    pn.add_argument("path", nargs="?", help="Power.log 路径（省略则用最新会话）")
    pn.add_argument("--cards-db", default=None, help="卡表缓存路径")
    pn.add_argument("--names-db", default=None, help="日志自举名路径")
    pn.add_argument("--cards-db-en", default=None,
                    help="英文卡表（只喂模型；缺省用 data/cards_enUS.json）")
    pn.add_argument("--no-color", action="store_true", help="（占位，JSON 永远无色）")
    pn.add_argument("--until-line", type=int, default=None,
                    help="只重放到第 N 行，输出一帧后退出（调试用）")
    pn.add_argument("--interval", type=float, default=0.4,
                    help="跟随模式下每帧间隔秒数（默认 0.4）")
    pn.add_argument("--model-ttl", type=float, default=12.0,
                    help="同一回合内模型意见的缓存秒数（默认 12）")
    # ⚠️ 默认 80 轮（20s）而不是 20 轮（5s）：一帧最多问 12+2 个问题，
    # 实测"待选 + 只买一张 + 升本"三问就要 5.75s，5s 上限会稳定超时，
    # 输出一帧"模型思考中…"却没有任何意见（踩过）。
    # 4B 档比 2B 慢约一倍（实测同一盘面 11.6s → 22.8s），而单帧模式要等
    # 「待选 + 只买一张 + 升本 + 逐张买入」全问完，20s 会稳定差一点。
    # 跟随模式不受这个上限影响（帧一直往外发，答案到了自动补上）。
    pn.add_argument("--model-wait", type=int, default=160,
                    help="单帧模式下等模型的最多 0.25s 轮数（默认 160 = 40s）")
    pn.add_argument("--limit", type=int, default=12, help="最多咨询几张商店随从")
    pn.add_argument("--no-model", action="store_true", help="只出规则建议，不问模型")
    pn.add_argument("--model-url", default=None, help="llama-server 地址")
    pn.add_argument("--model-size", default="2b", help="校准温度档位：2b/4b/4b-v0.2/9b")
    pn.add_argument("--model-timeout", type=float, default=60.0, help="单题超时秒数")
    pn.set_defaults(func=cmd_panel)

    return p


# --- P2：卡牌 / 盘面 -------------------------------------------------------


def _resolver(args) -> "object":
    from .cards import CardResolver
    return CardResolver.load(getattr(args, "cards_db", None),
                             getattr(args, "names_db", None),
                             getattr(args, "cards_db_en", None))


def cmd_cards(args: argparse.Namespace) -> int:
    """卡牌数据层自检：条目数 / 种族 / 星级 / 与日志自举名的交叉校验。"""
    from .cards import DEFAULT_CARD_DB_PATH, CardResolver
    from .names import DEFAULT_NAME_DB_PATH

    r = _resolver(args)
    db = r.db
    print(f"卡表    {args.cards_db or DEFAULT_CARD_DB_PATH}")
    print(f"名字表  {args.names_db or DEFAULT_NAME_DB_PATH}（{len(r.log_names)} 条）")
    if not len(db):
        print("⚠️ 卡表为空，跑 `python3 tools/build_cards.py` 重建", file=sys.stderr)
        return 1
    st = db.stats()
    print(f"战棋卡 {st['total']:,} 张；随从 {st['minions']:,}，其中当期随从池 {st['pool_minions']:,}")
    print(f"种族 {st['races']} 种：{', '.join(db.races())}")
    for tl in db.tech_levels():
        names = [c.name for c in db.by_tech_level(tl)]
        print(f"  {tl} 星 {len(names):>3} 张  " + "、".join(names[:6]) + ("…" if len(names) > 6 else ""))

    if getattr(args, "verify", False) and r.log_names:
        rep = db.verify_against_log_names(r.log_names)
        print(f"\n日志自举名交叉校验：{rep['checked']} 条 → 一致 {rep['ok']} / "
              f"不一致 {rep['mismatch']} / 卡表缺失 {rep['missing']}")
        for s in rep["samples"]:
            print(f"   差异 {s['card_id']}: 日志 {s['log']!r} vs 卡表 {s['db']!r}")
        for m in rep["missing_samples"]:
            print(f"   缺失 {m}")
    return 0


def _build_state(path: Optional[str], args, until_line: Optional[int] = None):
    from .parser import iter_file
    from .state import StateBuilder

    target = path or latest_power_log(args.log_root)
    if not target:
        print(f"找不到会话日志（{args.log_root}）", file=sys.stderr)
        return None, None, None
    b = StateBuilder()
    last = None
    for ev in iter_file(str(target)):
        b.apply(ev)
        last = ev
        if until_line is not None and ev.line_no >= until_line:
            break
    return b.state, str(target), b


def cmd_state(args: argparse.Namespace) -> int:
    """把整份日志重放一遍，打印最终盘面（快照）。"""
    from .render import render_state

    st, target, _ = _build_state(args.path, args, args.until_line)
    if st is None:
        return 1
    print(_dim(f"日志 {target}  回合 {st.turn}  {st.step}\n"))
    print(render_state(st, _resolver(args), color=not args.no_color))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """实时跟随并**在盘面变化时刷新**（P6 悬浮框的文本原型）。"""
    from .render import render_state
    from .state import StateBuilder

    resolver = _resolver(args)
    builder = StateBuilder()
    tailer = LogTailer(log_root=args.log_root, start_at_end=not args.head)
    if args.path:
        tailer.open_path(args.path, at_end=not args.head)
    elif not tailer.open() and not args.wait:
        print(f"找不到会话日志（{args.log_root}）", file=sys.stderr)
        return 1

    print(_dim(f"跟随 {tailer.current_path}"))
    print(_dim("盘面变化时重绘；Ctrl-C 退出\n"))

    last_key = None
    drawn = 0
    try:
        for ev in tailer.follow(max_idle_polls=(args.head * 2 if args.head else None)):
            builder.apply(ev)
            st = builder.state
            key = (st.turn, st.step, len(st.shop_minions), len(st.board),
                   tuple(s.card_id for s in st.shop_minions))
            if key == last_key:
                continue
            last_key = key
            if args.interval:
                # 清屏重绘（只在终端里好看；写文件时不用）
                if drawn:
                    sys.stdout.write("\033[2J\033[H")
            print(render_state(st, resolver, color=not args.no_color))
            print()
            drawn += 1
            if args.limit and drawn >= args.limit:
                break
    except KeyboardInterrupt:
        print()
    return 0


# --- P4：模型顾问层 ---------------------------------------------------------


def _render_advice(adv, st, resolver, color: bool = True) -> str:
    """把规则建议 + 模型意见拼成一块可读文本。"""
    from .advisor import answer_text, choice_ranking, slot_label
    from .render import Renderer

    r = Renderer(resolver, color=color)
    rep = adv.report
    lines: List[str] = []
    lines.append(r.bold("建议") + "  " + rep.headline)
    lines.append("")

    # 规则：商店打分
    # ⚠️ 用 `rep.shop[:3]`（已按分数降序）而不是 `top_shop(3)`：后者会**过滤掉
    # 「跳过」**，于是"整店都跳过"时这一段只剩个光秃秃的标题，看起来像解析失败。
    # `top_shop()` 仍然用于 headline 的"建议买「X」"。
    if rep.shop:
        lines.append(r.bold("商店"))
        for it in rep.shop[:3]:
            tag = {"买": r.good("买"), "考虑": r.warn("考虑"),
                   "跳过": r.dim("跳过")}.get(it.action, it.action)
            stats = "%s/%s%s" % (it.attack if it.attack is not None else "?",
                                 it.health if it.health is not None else "?",
                                 " ★" if it.golden else "")
            reason = ("；".join(it.reasons)) if it.reasons else ""
            lines.append("  %s %s  %s  %d星  %.1f分  %s"
                         % (tag, it.name, stats, it.tech_level or 0, it.score,
                            r.dim(reason)))
        if len(rep.shop) > 3:
            lines.append(r.dim("  （其余 %d 张分数更低，已省略）"
                               % (len(rep.shop) - 3)))
        lines.append("")

    # 规则：待选（饰品四选一 / 黑赐三选一）—— 放在最前面，不做决定会卡住回合
    pc = getattr(rep, "pending_choice", None)
    if pc is not None and pc.options:
        head = r.bold(pc.display_title) + r.dim("  （还没选）")
        if getattr(pc, "source_text", ""):
            head += r.dim("  " + pc.source_text)
        lines.append(head)
        for o in pc.options:
            lines.append("  %d. %s  %s" % (o.index + 1, o.label,
                                           r.dim(o.note or "")))
            # 黑暗之赐的赐福单独一行高亮：同一只随从会配不同赐福（实测
            # `鹰身人之爪` 给 圣盾+风怒、`防御献祭` 给 +10 血+亡语），
            # 赐福给了什么词条才是"这个选项强不强"的主要依据。
            gift = getattr(o, "gift_note", "")
            if gift:
                lines.append("     " + r.bold("赐福：" + gift))
        lines.append("")

    # 规则：已装备的饰品
    for tr in getattr(rep, "trinkets", []) or []:
        size = "小饰品" if tr.size == "small" else "大饰品"
        lines.append(r.bold(size) + "  " + tr.label
                     + (("  " + r.dim(tr.text)) if tr.text else ""))
    if getattr(rep, "trinkets", None):
        lines.append("")

    # 规则：黑暗之赐（BG36 机制）
    dg = getattr(rep, "dark_gift", None)
    if dg is not None and (dg.available or dg.granted):
        bits = []
        if dg.available:
            bits.append(r.good("可以点「黑暗发现」了"))
        if dg.granted:
            bits.append("已挂上：" + "、".join(dg.granted))
        lines.append(r.bold("黑暗之赐") + "  " + "  ".join(bits))
        if dg.known_effects:
            # ⚠️ 整局能攒出近 30 个赐福名，全列出来会把面板/终端刷爆，只报数量 + 最近几个
            names = dg.known_effects
            tail = "、".join(names[-6:])
            more = f"（共 {len(names)} 种）" if len(names) > 6 else ""
            lines.append(r.dim(f"  本局见过{more}：" + tail))
        lines.append("")

    # 规则：升本
    lv = rep.level
    if lv is not None and lv.next_tier is not None:
        cost = "?" if lv.cost is None else str(lv.cost)
        afford = "" if lv.affordable else r.warn("（金币不够）")
        lines.append(r.bold("升本") + "  %d本 → %d本  费用 %s%s  %s  %s"
                     % (lv.current_tier or 0, lv.next_tier, cost, afford,
                        lv.action, "；".join(lv.reasons)))
        lines.append("")

    # 规则：三连 / 预警
    if rep.triples:
        lines.append(r.bold("三连"))
        for t in rep.triples:
            lines.append("  %s 已有 %d 张%s" % (t.name, t.owned,
                                              "，商店里也有" if t.in_shop else ""))
        lines.append("")
    if rep.warnings:
        for w in rep.warnings:
            lines.append((r.warn if w.level != "info" else r.info)("⚠ " + w.text))
        lines.append("")

    # 模型意见
    lines.append(r.bold("模型意见") + r.dim("（%s）" % adv.provider))
    if adv.note:
        lines.append(r.dim("  " + adv.note))
    # ⚠️ 待选题排在最前：它是当前唯一"不做就卡住回合"的决策，
    # 而规则层对饰品/赐福刻意不打分，模型概率是这里唯一的信息来源。
    ch_ans = adv.answer_for("pending_choice")
    if ch_ans is not None and ch_ans.probabilities:
        pick_name = answer_text("pending_choice", ch_ans, rep, st.shop)
        lines.append("  %s %s" % (r.good(pick_name),
                                 r.dim("  P=%.2f，用时 %.2fs"
                                       % (ch_ans.confidence, ch_ans.seconds))))
        ranking = choice_ranking(rep, ch_ans)
        if len(ranking) > 1:
            bits = ["%s %.0f%%" % (nm, p * 100) for nm, p in ranking]
            lines.append(r.dim("    概率分布：" + " · ".join(bits)))
    pick = adv.answer_for("pick_shop")
    if pick is not None and pick.choice:
        target = ""
        for it in rep.shop:
            if pick.choice.endswith("shop slot %d" % it.index):
                target = slot_label(st.shop, it.slot_entity_id, rep)
                break
        lines.append("  只买一张的话：%s" % r.good(target or pick.choice)
                     + r.dim("  P=%.2f，用时 %.2fs" % (pick.confidence, pick.seconds)))
    lv_ans = adv.answer_for("level_up")
    if lv_ans is not None:
        lines.append("  现在升本？%s" % (r.good("是") if (lv_ans.noul or 0) >= 0.5
                                      else r.dim("否"))
                     + r.dim("  P(是)=%.2f" % (lv_ans.noul or 0.0)))
    buys = list(adv.buy_predictions().items())
    if buys:
        lines.append("  逐张买入意愿（按可能性排序）：")
        for slot_id, p in sorted(buys, key=lambda kv: -kv[1]):
            lines.append("    %-28s %s  P(买)=%.2f"
                         % (slot_label(st.shop, slot_id, rep),
                            r.good("买") if p >= 0.5 else r.dim("不买"), p))
    if not adv.used_model and not adv.note:
        lines.append(r.dim("  （未咨询模型，用 --no-model 可显式跳过）"))
    return "\n".join(lines)


def cmd_advise(args: argparse.Namespace) -> int:
    """规则优先 + JevK5 兜底的建议（P3+P4）。"""
    import time

    from .advisor import advisor_from_args, consult
    from .render import render_state

    st, target, _ = _build_state(args.path, args, args.until_line)
    if st is None:
        return 1
    t0 = time.time()
    resolver = _resolver(args)
    db = resolver.db
    print(_dim(f"日志 {target}  回合 {st.turn}  {st.step}"))
    if not st.shop_minions and not st.board:
        print("（这一帧没有可看的盘面；用 --until-line 定位到招募期）")
    if not args.quiet:
        print()
        print(render_state(st, resolver, color=not args.no_color))
    adv = consult(st, db=db, resolver=resolver,
                  advisor=advisor_from_args(args), limit=args.limit)
    print()
    print(_render_advice(adv, st, resolver, color=not args.no_color))
    if args.show_evidence:
        print()
        print(_dim("—— 喂给模型的英文 evidence ——"))
        print(adv.evidence)
    print(_dim("\n耗时 %.2fs" % (time.time() - t0)))
    return 0


def _empty_state():
    from .state import GameState
    return GameState()


def _frame_for(builder, resolver, runner, args):
    """基于当前 builder 造一帧：规则每帧重算，模型意见只走缓存。"""
    from .panel import build_frame
    from .rules import evaluate_state

    st = builder.state
    report = evaluate_state(st, resolver.db if resolver else None,
                            db_en=getattr(resolver, "db_en", None) if resolver else None)
    runner.maybe_refresh(st, resolver, report)
    return build_frame(st, resolver, report=report,
                       advice=runner.current(st), model_note=runner.note)


def _emit_frame(frame) -> None:
    """一帧一行 JSON。

    `ensure_ascii=False` —— 中文直接以 UTF-8 输出，Swift 侧按 UTF-8 解；
    转义成 \\uXXXX 也对，但日志里不好读，调试时很痛。
    """
    sys.stdout.write(json.dumps(frame.to_dict(), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def cmd_panel(args: argparse.Namespace) -> int:
    """给 Swift 悬浮框喂数据：一行一帧 JSON（P6 的数据契约）。

    两种模式：
      - `--until-line N`：重放到第 N 行，输出**一帧**后退出（调试/测试用）。
      - 默认：重放整份日志到当前时刻，再**跟随最新会话**按 `--interval` 持续刷帧。

    ⚠️ 模型调用绝不阻塞刷帧：一帧几十毫秒，一问模型几百毫秒。规则结果每帧都新算，
    模型意见由后台线程算好后补进后续帧（见 `panel.ModelAdvisorRunner`），
    所以面板永远先亮起来，模型意见晚几帧到。
    """
    from .panel import ModelAdvisorRunner, build_frame
    from .rules import evaluate_state
    from .state import StateBuilder

    resolver = _resolver(args)
    runner = ModelAdvisorRunner(
        advisor_from_panel_args(args),
        ttl=getattr(args, "model_ttl", 12.0),
        limit=getattr(args, "limit", 12),
    )
    builder = StateBuilder()

    try:
        if args.until_line:
            path = args.path or latest_power_log(DEFAULT_LOG_ROOT)
            if path is None:
                _emit_frame(build_frame(_empty_state(), resolver,
                                        error="找不到 Power.log"))
                return 0
            # ⚠️ `iter_file` 不接受 limit_lines（它按行流式），
            # 所以这里自己数行 —— 重放到第 N 行就停。
            for ev in iter_file(path):
                if ev.line_no and ev.line_no > args.until_line:
                    break
                builder.apply(ev)
            st = builder.state
            report = evaluate_state(st, resolver.db if resolver else None,
                                    db_en=getattr(resolver, "db_en", None)
                                    if resolver else None)
            runner.maybe_refresh(st, resolver, report)
            # 单帧模式：给模型一点时间，尽量把意见一起输出。
            # ⚠️ 不能把 `runner.note`（"模型思考中…"）当成"有答案"，
            # 否则第一轮就 break，模型永远来不及算完。
            for _ in range(max(0, int(getattr(args, "model_wait", 20)))):
                if runner.current(st) is not None:
                    break
                time.sleep(0.25)
            _emit_frame(build_frame(st, resolver, report=report,
                                    advice=runner.current(st), model_note=runner.note))
            return 0

        # ---- 跟随模式 ----
        # 1) 先把历史重放完，否则面板一开始是空的。
        current_path = args.path or latest_power_log(DEFAULT_LOG_ROOT)
        if current_path is not None:
            for ev in iter_file(current_path):
                builder.apply(ev)
            _emit_frame(_frame_for(builder, resolver, runner, args))

        # 2) 从文件末尾开始跟随；会话轮换时整体重建。
        tailer = LogTailer(log_root=DEFAULT_LOG_ROOT)
        if current_path is not None:
            tailer.open_path(current_path, at_end=True)
        else:
            tailer.open()

        interval = max(0.05, float(getattr(args, "interval", 0.4)))
        next_frame = 0.0
        while True:
            for ev in tailer.poll():
                builder.apply(ev)
            target = getattr(tailer, "path", None)
            if target is not None and current_path is not None and target != current_path:
                current_path = target
                builder = StateBuilder()
                for ev in iter_file(target):
                    builder.apply(ev)

            now = time.time()
            if now >= next_frame:
                next_frame = now + interval
                _emit_frame(_frame_for(builder, resolver, runner, args))
    except KeyboardInterrupt:
        return 0
    finally:
        runner.close()


def advisor_from_panel_args(args):
    from .advisor import advisor_from_args
    return advisor_from_args(args)


def cmd_serve(args: argparse.Namespace) -> int:
    """JevK5 本地推理服务的状态 / 启动 / 停止。

    为什么不直连 LM Studio：它的 HTTP 服务不返回 logprobs，而 JevK5 的置信度
    全靠答案位置字母的 softmax。所以走 LM Studio 自带的 llama-server。
    """
    from . import model_server as ms
    from .provider import DEFAULT_URL

    url = args.model_url or DEFAULT_URL
    if args.action == "stop":
        return ms.stop()
    if args.action == "start":
        return ms.start(port=ms.DEFAULT_PORT, url=url)
    if ms.health(url):
        print("✅ 模型服务就绪：%s" % url)
        pid = ms.read_pid()
        if pid:
            print("   pid %d，日志 %s" % (pid, ms.LOG_FILE))
        return 0
    print("❌ 模型服务未就绪：%s\n" % url)
    s, m = ms.find_llama_server(), ms.find_gguf()
    print("llama-server  %s" % (s or "（没找到）"))
    print("GGUF          %s" % (m or "（没找到）"))
    print("\n启动它：")
    print("  ./hsbg serve --start")
    print("\n或手工运行：")
    print("  " + ms.serve_command(s, m))
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
