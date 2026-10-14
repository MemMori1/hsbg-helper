"""展示层（P2/P3 的文本形态）。

把 `GameState` 渲染成人能读的紧凑面板。这是 P6 Swift 悬浮框的**内容原型**：
悬浮框上要放什么、按什么顺序放，先在这里定下来，再用 SwiftUI 复刻。

设计目标（对齐 FR-6.1）：竖排紧凑、一屏看完、颜色分级。宽度对齐悬浮框的
320–360 pt，所以正文行控制在 ~46 个全宽字符以内（中文按 2 计）。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .cards import CardResolver
from .state import Entity, GameState, Seat, ShopSlot

__all__ = ["Renderer", "render_state", "render_seat_line", "panel_width"]

#: 悬浮框目标宽度（pt）。正文按此换算可见字符数。
PANEL_WIDTH_PT = 340
panel_width = 46

_RACE_ZH = {
    "BEAST": "野兽",
    "MECHANICAL": "机械",
    "UNDEAD": "亡灵",
    "DRAGON": "龙",
    "PIRATE": "海盗",
    "DEMON": "恶魔",
    "MURLOC": "鱼人",
    "QUILBOAR": "野猪人",
    "ELEMENTAL": "元素",
    "NAGA": "纳迦",
    "ABERRATION": "异变",
    "ALL": "全部",
}


def display_width(s: str) -> int:
    """按终端/悬浮框视觉宽度计：CJK 算 2，其余算 1。"""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x1100 and _is_wide(ch) else 1
    return w


def _is_wide(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1100 <= o <= 0x115F
        or 0x2E80 <= o <= 0xA4CF
        or 0xAC00 <= o <= 0xD7A3
        or 0xF900 <= o <= 0xFAFF
        or 0xFE30 <= o <= 0xFE6F
        or 0xFF00 <= o <= 0xFF60
        or 0xFFE0 <= o <= 0xFFE6
        or 0x1F300 <= o <= 0x1FAFF
    )


def pad_to(s: str, width: int) -> str:
    """按视觉宽度右侧补空格（可用于双栏排版）。"""
    return s + " " * max(0, width - display_width(s))


class Renderer:
    """把 `GameState` 渲染成文本面板。"""

    def __init__(self, resolver: Optional[CardResolver] = None, color: bool = True):
        self.resolver = resolver if resolver is not None else CardResolver.bundled()
        self.color = color

    # -- 着色 ----------------------------------------------------------
    def _c(self, s: str, code: str) -> str:
        if not self.color:
            return s
        return f"\033[{code}m{s}\033[0m"

    def dim(self, s: str) -> str:
        return self._c(s, "2")

    def bold(self, s: str) -> str:
        return self._c(s, "1")

    def gold(self, s: str) -> str:
        return self._c(s, "33")      # 黄

    def good(self, s: str) -> str:
        return self._c(s, "32")      # 绿

    def warn(self, s: str) -> str:
        return self._c(s, "31")      # 红

    def info(self, s: str) -> str:
        return self._c(s, "36")      # 青

    # -- 名字 ----------------------------------------------------------
    def name_of(self, card_id: Optional[str]) -> str:
        return self.resolver.name_of(card_id) or (card_id or "?")

    def describe(self, card_id: Optional[str]) -> str:
        return self.resolver.describe(card_id) or (card_id or "?")

    def race_zh(self, race: str) -> str:
        return _RACE_ZH.get((race or "").upper(), race or "")

    # -- 部件 ----------------------------------------------------------
    def minion_text(self, e: Entity) -> str:
        """一只随从：`名字 攻/血 三连标记`。"""
        nm = self.name_of(e.card_id)
        atk = e.attack
        hp = e.health
        golden = "★" if e.golden else ""
        taunt = "†" if str(e.tags.get("TAUNT", "0")) not in ("0", "") else ""
        shield = "◇" if str(e.tags.get("DIVINE_SHIELD", "0")) not in ("0", "") else ""
        body = f"{atk}/{hp}" if atk is not None and hp is not None else ""
        return f"{golden}{nm}{taunt}{shield} {body}".strip()

    def slot_text(self, s: ShopSlot) -> str:
        nm = self.name_of(s.card_id)
        bits = [nm]
        if s.tech_level:
            bits.append(f"{s.tech_level}星")
        if s.attack is not None and s.health is not None:
            bits.append(f"{s.attack}/{s.health}")
        if s.golden:
            bits.append("★")
        return " ".join(bits)

    def seat_line(self, s: Seat) -> str:
        """一个对手：`P1 英雄名 血 等级`。"""
        mark = "*" if s.is_local else " "
        hp = s.effective_health
        hp_s = f"{hp:>2}" if hp is not None else " ?"
        tl = f"{s.tech_level}本" if s.tech_level else " ?本"
        flags = ""
        if s.died_last_combat:
            flags += " 出局?"
        if s.won_last_combat:
            flags += " 胜"
        return f"P{s.player_id}{mark} {self.name_of(s.hero_card_id):<12} {hp_s}血 {tl}{flags}"

    def panel(self, st: GameState) -> str:
        """整块面板（悬浮框要显示的全部内容）。"""
        lines: List[str] = []
        sep = self.dim("─" * panel_width)

        # 1. 回合 / 阶段 / 金币
        header = f"回合 {st.turn}"
        if st.in_recruit_phase:
            phase = self.good("招募")
        else:
            phase = self.warn(st.step or "?")
        local = st.local_seat
        gold = f"金币 {local.gold}" if local and local.gold is not None else "金币 ?"
        lines.append(f"{self.bold(header)}  {phase}  {self.gold(gold)}")

        # 2. 商店建议位（P3 填内容，先占位）
        shop = st.shop_minions
        lines.append(sep)
        buyable = [s for s in shop if not s.empty]
        lines.append(self.info(f"商店（{len(buyable)} 张）"))
        if not buyable:
            lines.append(self.dim("  （未在招募期或无数据）"))
        for i, s in enumerate(buyable, 1):
            lines.append(f"  {i}. {self.slot_text(s)}")

        # 3. 自家场面
        lines.append(sep)
        lines.append(self.info(f"自家场面（{len(st.board)}）"))
        if not st.board:
            lines.append(self.dim("  （空）"))
        for i, e in enumerate(st.board, 1):
            lines.append(f"  {i}. {self.minion_text(e)}")

        # 4. 手牌
        if st.hand:
            lines.append(sep)
            names = "、".join(self.name_of(e.card_id) for e in st.hand)
            lines.append(self.info(f"手牌（{len(st.hand)}）") + f" {names}")

        # 5. 对手血线（按血量升序，最危险的在最上面）
        lines.append(sep)
        opps = [s for s in st.seats.values() if not s.is_local]
        opps.sort(key=lambda s: (s.effective_health if s.effective_health is not None else 999,
                                 s.player_id))
        lines.append(self.info("对手血线"))
        for s in opps:
            hp = s.effective_health
            line = self.seat_line(s)
            lines.append("  " + (self.warn(line) if hp is not None and hp <= 10 else line))

        return "\n".join(lines)


def render_state(st: GameState, resolver: Optional[CardResolver] = None,
                 color: bool = True) -> str:
    return Renderer(resolver=resolver, color=color).panel(st)


def render_seat_line(s: Seat, resolver: Optional[CardResolver] = None) -> str:
    return Renderer(resolver=resolver).seat_line(s)
