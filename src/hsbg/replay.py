"""复盘回放（P7）：把面板帧落成"一局一个 JSON"，离线圈看。

数据从哪来
----------
Swift 悬浮框跑的就是 `hsbg panel` 的跟随模式（`DataFeed.swift` 里
`hsbg panel …`），面板每 `--interval` 秒出一帧。录制器挂在帧循环上：
**开着悬浮框打游戏就自动落盘**，不需要任何额外命令。

一局一个文件：`replays/20261016-194012-钩牙船长.json`

    {
      "version": 1,
      "hero": "钩牙船长",              # 本地玩家英雄（优先卡表名，缺了退回日志名）
      "hero_card_id": "TB_BaconShop_HERO_91",
      "started_at": "2026-10-16 19:40:12",
      "ended_at":   "2026-10-16 20:05:33",
      "partial": false,                # true = 还没看到"下一局开始"，可能没录到收尾
      "turns": 12,                     # 最后一帧的回合数
      "final": {"turn": 12, "health": -3},
      "choices": [ ...state.ChoiceRecord... ],   # 本局选一历史（饰品/黑赐/换牌）
      "frames":  [ ...panel.PanelFrame.to_dict()... ]  # 去重后的帧
    }

三个坑（都踩过才这么写）
------------------------
- **新局判据用"回合回卷"**：`StateBuilder` 在新号 `CREATE_GAME` 时重建 GameState，
  下一帧 `turn` 会小于上一帧（12 → 1）。⚠️ 判断必须发生在更新内部记忆**之前**，
  否则新局的空 `choice_log` 会盖掉正要落盘的旧局数据。
- **必须周期性落盘**：悬浮框是长期进程，App 退出走的是 SIGTERM，Python 的
  `finally` 不会执行。只在"局终/退出"写盘的话，中途被杀 = 整局白录。
  这里每 ~15s 原子重写一次，最多丢最后几秒。
- **去重按整帧 JSON**：面板每 0.4s 无条件出帧，绝大多数帧与上一帧完全相同；
  不去重一局能写十几 MB。

降级约定
--------
- 目录不可写 / 磁盘满 → **静默放弃这一帧**：录像是附带品，绝不能影响面板出帧。
- 文件坏 / 不是本模块的格式 → 列表里跳过、回看时报错退出（不猜数据）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_REPLAY_DIR

__all__ = [
    "ReplayRecorder",
    "list_replays",
    "load_replay",
    "render_replay",
    "resolve_target",
]

#: 落盘格式版本；将来改结构时回看端按这个分支。
REPLAY_VERSION = 1

#: 进行中的对局每多少秒原子重写一次（见模块头"必须周期性落盘"）。
CHECKPOINT_SECONDS = 15.0


def _safe_name(text: str, limit: int = 24) -> str:
    """英雄名 → 文件名安全片段（中文保留，其余换成 `-`）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (text or "").strip())
    s = s.strip("-")
    return s[:limit] or "unknown"


def _choices_to_dicts(choices) -> List[Dict[str, Any]]:
    """`ChoiceRecord`（dataclass）列表 → 可序列化字典列表。

    容忍已在字典形态的输入（测试与将来换类型都用得上），坏对象退化成字符串。
    """
    out: List[Dict[str, Any]] = []
    for c in choices or []:
        if isinstance(c, dict):
            out.append(dict(c))
        elif is_dataclass(c):
            out.append(asdict(c))
        else:
            out.append({"selected_name": str(c)})
    return out


class ReplayRecorder:
    """把面板帧按局攒起来落盘。

    用法（`cmd_panel` 跟随模式）：:

        rec = ReplayRecorder()
        rec.note(frame.to_dict(), choices=st.choice_log, hero=...)
        ...
        rec.flush()          # 退出时兜底

    `note()` 在"这一帧意味着上一局结束"时返回落盘路径（新局开头的帧）。
    """

    def __init__(self, out_dir: Optional[Path] = None, enabled: bool = True):
        self.out_dir = Path(out_dir) if out_dir else DEFAULT_REPLAY_DIR
        self.enabled = bool(enabled)
        self._clear()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _clear(self) -> None:
        self._frames: List[Dict[str, Any]] = []
        self._last_key: Optional[str] = None
        self._last_turn = 0
        self._health: Optional[int] = None
        self._choices: List[Dict[str, Any]] = []
        self._hero = ""
        self._hero_card_id = ""
        self._started_at = ""
        self._ended_at = ""
        self._path: Optional[Path] = None
        self._last_write = 0.0

    def _start_game(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        # ⚠️ 文件名在**第一次落盘时**定下来并冻结：英雄名之后才认出来时
        # 不能改名，否则同一局会出现两个文件。
        self._stamp = stamp

    def _write(self, partial: bool) -> Optional[Path]:
        """原子落盘。任何 OSError 都吞掉（录像绝不能让面板崩）。"""
        if not self._frames:
            return None
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            if self._path is None:
                self._path = self.out_dir / (
                    "%s-%s.json" % (self._stamp, _safe_name(self._hero)))
            doc = {
                "version": REPLAY_VERSION,
                "hero": self._hero,
                "hero_card_id": self._hero_card_id,
                "started_at": self._started_at,
                "ended_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "partial": bool(partial),
                "turns": self._last_turn,
                "final": {"turn": self._last_turn, "health": self._health},
                "choices": self._choices,
                "frames": self._frames,
            }
            tmp = self._path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False)
            os.replace(tmp, self._path)
            self._last_write = time.time()
            return self._path
        except OSError:
            return None

    def _finalize(self) -> Optional[Path]:
        """上一局收尾（看到新局开始的帧）：落盘 + 清场。"""
        path = self._write(partial=False)
        self._clear()
        return path

    # ------------------------------------------------------------------
    # 对外的两个动作
    # ------------------------------------------------------------------
    def note(self, frame: Dict[str, Any], *, choices=None,
             hero: str = "", hero_card_id: str = "") -> Optional[Path]:
        """喂一帧。若这帧是"新局的第一帧"，先把上一局落盘并返回其路径。"""
        if not self.enabled:
            return None
        try:
            turn = int(frame.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0

        flushed: Optional[Path] = None
        # ⚠️ 先判断回卷、后更新记忆（见模块头第 1 个坑）。
        if turn > 0 and 0 < turn < self._last_turn:
            flushed = self._finalize()
        if turn <= 0:
            return flushed                       # 选人/载入帧没有盘面，不入档

        if not self._started_at:
            self._start_game()
        if hero:
            self._hero = hero
            self._hero_card_id = hero_card_id or self._hero_card_id
        if choices is not None:
            self._choices = _choices_to_dicts(choices)
        self._last_turn = turn
        if frame.get("health") is not None:
            self._health = frame.get("health")

        key = json.dumps(frame, sort_keys=True, ensure_ascii=False)
        if key != self._last_key:
            self._frames.append(frame)
            self._last_key = key
            if time.time() - self._last_write >= CHECKPOINT_SECONDS:
                self._write(partial=True)
        return flushed

    def flush(self) -> Optional[Path]:
        """进程退出前的兜底落盘。

        ⚠️ `partial` 保持 true：退出时**不知道**对局是否打完（用户可能打到一半
        关 App），只有"看到下一局开始"才算完结（那条路走 `_finalize`）。
        """
        if not self.enabled or not self._frames:
            self._clear()
            return None
        path = self._write(partial=True)
        self._clear()
        return path


# ---------------------------------------------------------------------------
# 回看端：列表 / 读取 / 渲染
# ---------------------------------------------------------------------------

def _read_doc(path: Path) -> Optional[Dict[str, Any]]:
    """读一个录像文件；坏了/不是我们的格式就返回 None（不猜）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("frames"), list):
        return None
    return doc


def list_replays(out_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """目录里所有能解析的录像，新→旧。坏文件跳过（列表里不出现）。"""
    d = Path(out_dir) if out_dir else DEFAULT_REPLAY_DIR
    items: List[Dict[str, Any]] = []
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        files = []
    for p in files:
        doc = _read_doc(p)
        if doc is None:
            continue
        items.append({
            "path": p,
            "hero": doc.get("hero") or "?",
            "started_at": doc.get("started_at") or "",
            "ended_at": doc.get("ended_at") or "",
            "turns": int(doc.get("turns") or 0),
            "frames": len(doc.get("frames") or []),
            "choices": len(doc.get("choices") or []),
            "partial": bool(doc.get("partial", False)),
        })
    items.sort(key=lambda it: (it["started_at"], str(it["path"])), reverse=True)
    return items


def load_replay(path) -> Dict[str, Any]:
    """读一个录像；坏文件抛 `ValueError`（调用方负责打印）。"""
    p = Path(path).expanduser()
    doc = _read_doc(p)
    if doc is None:
        raise ValueError("不是有效的录像文件：%s" % p)
    return doc


def resolve_target(arg: str, out_dir: Optional[Path] = None) -> Path:
    """`last` / 序号（0=最新）/ 文件路径 → 录像路径。找不到抛 `FileNotFoundError`。"""
    text = (arg or "").strip()
    if text in ("last", "latest", "最新"):
        items = list_replays(out_dir)
        if not items:
            raise FileNotFoundError("还没有录到任何对局")
        return items[0]["path"]
    if text.lstrip("-").isdigit() and text.lstrip("-"):
        items = list_replays(out_dir)
        idx = int(text)
        if not items or not (-len(items) <= idx < len(items)):
            raise FileNotFoundError("序号 %s 超出范围（共 %d 局）" % (text, len(items)))
        return items[idx]["path"]
    p = Path(text).expanduser()
    if not p.is_file():
        raise FileNotFoundError("找不到文件：%s" % p)
    return p


def _frame_line(f: Dict[str, Any]) -> str:
    """一帧的紧凑单行。空的部分不出现（不然全是 `· · ·`）。"""
    bits: List[str] = []
    if f.get("gold") is not None:
        bits.append("%s 费" % f["gold"])
    if f.get("health") is not None:
        bits.append("%s 血" % f["health"])
    if f.get("tavern_tier"):
        bits.append("%s 级" % f["tavern_tier"])
    names = [str(s.get("name") or "?") for s in (f.get("shop") or [])]
    if names:
        bits.append("商店：" + " ".join(names))
    dec = f.get("decision") or {}
    if dec:
        act = str(dec.get("action") or "").strip()
        tgt = str(dec.get("target") or "").strip()
        bits.append("决策：%s%s" % (act, ("「%s」" % tgt) if tgt else ""))
    pend = f.get("pending_choice") or {}
    if pend:
        star = ""
        for o in pend.get("options") or []:
            if o.get("recommended"):
                star = "（★ %s）" % (o.get("name") or "?")
                break
        bits.append("待选：%s%s" % (pend.get("title") or pend.get("kind") or "?", star))
    for a in (f.get("model_answers") or []):
        ans = str(a.get("answer") or "").strip()
        if ans:
            bits.append("模型：%s" % ans)
            break
    for w in (f.get("warnings") or []):
        txt = str(w.get("text") or "").strip()
        if txt:
            bits.append("⚠ " + txt)
            break
    return " · ".join(bits) if bits else "(空帧)"


def render_replay(doc: Dict[str, Any], color: bool = False) -> str:
    """把一局录像排成可读文本（`color=True` 时上 ANSI 色）。"""
    def c(code: str, t: str) -> str:
        return "\033[%sm%s\033[0m" % (code, t) if color else t

    hero = doc.get("hero") or "?"
    turns = int(doc.get("turns") or 0)
    lines: List[str] = [c("1", "══ %s · %d 回合 ══" % (hero, turns))]

    meta: List[str] = []
    if doc.get("started_at"):
        meta.append(str(doc["started_at"]))
    if doc.get("ended_at"):
        meta.append("→ " + str(doc["ended_at"]))
    if doc.get("partial"):
        meta.append("（未录到下一局开始，结尾可能不完整）")
    if meta:
        lines.append("  " + c("2", "  ".join(meta)))

    fin = doc.get("final") or {}
    if fin:
        hp = fin.get("health")
        lines.append("  最终：回合 %s · 血量 %s" % (
            fin.get("turn") if fin.get("turn") is not None else "?",
            hp if hp is not None else "?"))

    choices = doc.get("choices") or []
    if choices:
        lines.append("")
        lines.append(c("1", "选一历史："))
        for ch in choices:
            #: 标签优先用中文 `source_name`（如「小饰品」），kind 是英文标识符兜底。
            label = ch.get("source_name") or ch.get("kind") or "?"
            name = ch.get("selected_name") or "?"
            lines.append("  · %s → 「%s」" % (label, name))

    lines.append("")
    last_turn = None
    for f in doc.get("frames") or []:
        if not isinstance(f, dict):
            continue
        turn = f.get("turn") or 0
        if turn != last_turn:
            lines.append(c("1", "── 回合 %s ──" % turn))
            last_turn = turn
        lines.append("  " + _frame_line(f))
    if not doc.get("frames"):
        lines.append("（没有帧数据）")
    return "\n".join(lines)
