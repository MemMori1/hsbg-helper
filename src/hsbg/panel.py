"""盘面 → 悬浮框的一帧 JSON（P6 的数据契约）。

这一层刻意单独存在，理由：

1. **render.py 是给人看的文本，这里是给机器看的 JSON。** 两边信息层级要对齐，
   但格式需求完全不同（文本要宽字符对齐，JSON 要字段稳定）。
2. **模型调用必须和帧率解耦。** JevK5 一次提问 0.3–0.8 s，一帧却要 5–10 Hz；
   同步调用会把面板卡死。所以这里用「过期时间 + 后台线程」把模型意见做成
   *可选增强*：第一帧先出纯规则结果，模型意见算完了再补进后续帧。

字段命名与 Swift 侧的 `PanelContentState(json:)` 一一对应，改这里要同步改那边。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .advisor import (Advice, answer_text, choice_ranking, consult,
                      question_text)
from .cards import CardResolver
from .state import GameState

# ---------------------------------------------------------------------------
# 单个模型的意见要缓存多久
# ---------------------------------------------------------------------------

#: 同一回合内，模型意见最多每 N 秒重算一次。
#: 太小会拖垮面板（每条提问 0.3–0.8 s），太大则回合变了还在显示旧建议。
DEFAULT_MODEL_TTL = 12.0

#: 一次最多问几张商店随从。2B 模型每问 0.3–0.8 s，12 张 ≈ 6 s，已经偏久。
DEFAULT_BUY_LIMIT = 12

PHASE_ZH = {
    "MAIN_READY": "招募",
    "MAIN_START_TRIGGERS": "招募",
    "MAIN_START": "招募",
    "MAIN_ACTION": "招募",
    "MAIN_COMBAT": "战斗",
    "COMBAT": "战斗",
    "MAIN_END": "结算",
    "MAIN_CLEANUP": "结算",
    "MAIN_NEXT": "结算",
    "FINAL_GAMEOVER": "结束",
}


def phase_text(step: str) -> str:
    return PHASE_ZH.get(step or "", step or "")


# ---------------------------------------------------------------------------
# 后台模型意见
# ---------------------------------------------------------------------------


@dataclass
class _ModelCache:
    """最近一次模型意见，以及它的有效期。"""

    advice: Optional[Advice] = None
    computed_at: float = 0.0
    turn: int = -1
    running: bool = False
    error: str = ""

    def valid_for(self, turn: int, ttl: float, now: float) -> bool:
        if self.advice is None or self.turn != turn:
            return False
        return (now - self.computed_at) < ttl


class ModelAdvisorRunner:
    """在后台线程里问模型，结果供后续帧使用。

    为什么不是每次刷帧都问：一帧几十毫秒，一问几百毫秒。
    为什么不是只在回合开始时问：招募期内买/卖会改变商店，12 s 的 TTL 是个折中。
    """

    def __init__(self, advisor, ttl: float = DEFAULT_MODEL_TTL,
                 limit: int = DEFAULT_BUY_LIMIT) -> None:
        self.advisor = advisor
        self.ttl = ttl
        self.limit = limit
        self._cache = _ModelCache()
        self._lock = threading.Lock()

    @property
    def note(self) -> str:
        with self._lock:
            if self._cache.error:
                return self._cache.error
            if self._cache.running:
                return "模型思考中…"
            if self.advisor is None:
                return ""
            return ""

    def maybe_refresh(self, st: GameState, resolver: Optional[CardResolver],
                      report) -> None:
        """需要就起一个后台线程去问模型；绝不阻塞调用方。"""
        if self.advisor is None:
            return
        now = time.time()
        with self._lock:
            if self._cache.running:
                return
            if self._cache.valid_for(st.turn, self.ttl, now):
                return
            self._cache.running = True

        # snapshot：后台线程只读，拿一份独立引用避免与主循环竞争。
        snapshot = st

        def _work() -> None:
            try:
                adv = consult(snapshot, resolver=resolver, advisor=self.advisor,
                              limit=self.limit, engine=None)
                with self._lock:
                    self._cache.advice = adv
                    self._cache.computed_at = time.time()
                    self._cache.turn = snapshot.turn
                    self._cache.error = "" if adv.used_model else (adv.note or "")
            except Exception as exc:  # noqa: BLE001 - 绝不能让后台线程打死面板
                with self._lock:
                    self._cache.error = "模型调用失败：%s" % exc
            finally:
                with self._lock:
                    self._cache.running = False

        threading.Thread(target=_work, name="hsbg-model", daemon=True).start()

    def current(self, st: GameState) -> Optional[Advice]:
        with self._lock:
            if self._cache.advice is None:
                return None
            if self._cache.turn not in (-1, st.turn):
                return None
            return self._cache.advice

    def close(self) -> None:
        if self.advisor is not None:
            try:
                self.advisor.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 一帧
# ---------------------------------------------------------------------------


@dataclass
class PanelFrame:
    """一帧的全部信息。`to_dict()` 就是 JSON Lines 的那一行。"""

    turn: int = 0
    phase: str = ""
    gold: int = 0
    health: int = 0
    tavern_tier: int = 0
    headline: str = ""
    shop: List[Dict[str, Any]] = field(default_factory=list)
    board: List[Dict[str, Any]] = field(default_factory=list)
    hand: List[str] = field(default_factory=list)
    opponents: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    #: 「黑暗之赐」状态（rules.DarkGift 的字典形式），没有该机制时为 None
    dark_gift: Optional[Dict[str, Any]] = None
    #: 当前等待玩家做的选一（饰品四选一 / 黑赐三选一）
    pending_choice: Optional[Dict[str, Any]] = None
    #: 已装备的饰品（小/大）
    trinkets: List[Dict[str, Any]] = field(default_factory=list)
    #: 本局黑赐用过几次、分别给了谁
    dark_gift_uses: List[Dict[str, Any]] = field(default_factory=list)
    level_advice: Optional[Dict[str, Any]] = None
    model_answers: List[Dict[str, Any]] = field(default_factory=list)
    model_note: str = ""
    stale: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "turn": self.turn,
            "phase": self.phase,
            "gold": self.gold,
            "health": self.health,
            "tavern_tier": self.tavern_tier,
            "headline": self.headline,
            "shop": self.shop,
            "board": self.board,
            "hand": self.hand,
            "opponents": self.opponents,
            "warnings": self.warnings,
            "model_answers": self.model_answers,
            "model_note": self.model_note,
            "stale": self.stale,
        }
        if self.dark_gift is not None:
            out["dark_gift"] = self.dark_gift
        if self.pending_choice is not None:
            out["pending_choice"] = self.pending_choice
        if self.trinkets:
            out["trinkets"] = self.trinkets
        if self.dark_gift_uses:
            out["dark_gift_uses"] = self.dark_gift_uses
        if self.level_advice is not None:
            out["level_advice"] = self.level_advice
        if self.error:
            out["error"] = self.error
        return out


def _int_or_none(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(e, tag: str) -> bool:
    """关键词判定。

    ⚠️ 用 `not in ("0", "")` 而不是 `== "1"`：日志里出现过 `value=2`
    （叠加层数）这种形态，只认 "1" 会漏掉一部分带嘲讽/圣盾的随从。
    """
    return str(e.tags.get(tag, "0")) not in ("0", "")


def _minion_row(e, resolver: Optional[CardResolver]) -> Dict[str, Any]:
    return {
        "name": resolver.name_of(e.card_id) if resolver else (e.card_id or "?"),
        "attack": e.attack or 0,
        "health": e.health or 0,
        # ⚠️ 三连看 `Entity.golden`（读的是 `PREMIUM`），不是 `BACON_IS_GOLDEN`。
        "golden": bool(e.golden),
        "taunt": _flag(e, "TAUNT"),
        "divine_shield": _flag(e, "DIVINE_SHIELD"),
    }


def build_frame(st: GameState,
                resolver: Optional[CardResolver] = None,
                report=None,
                advice: Optional[Advice] = None,
                stale: bool = False,
                error: str = "",
                model_note: str = "") -> PanelFrame:
    """把盘面 + 规则结果 +（可选的）模型意见拼成一帧。

    `report` 为空时自己跑一次规则引擎；`advice` 为空时只输出规则部分。
    """
    from .rules import evaluate_state  # 局部导入，避免 rules ↔ panel 循环引用

    # ⚠️ 卡表要从 resolver 挖出来并**共用一份**：`evaluate_state` 和后面的自定义
    # 报告都靠它算关键词标签。分头传会让"有 resolver"和"没 resolver"两条路径
    # 行为不一致（实测面板里 `keywords` 会是空的，而 CLI 里有）。
    db = resolver.db if resolver is not None else None
    db_en = resolver.db_en if resolver is not None else None
    if report is None:
        report = evaluate_state(st, db, db_en=db_en)

    seat = st.local_seat
    frame = PanelFrame(
        turn=st.turn,
        phase=phase_text(st.step),
        gold=(seat.gold if seat and seat.gold is not None else 0),
        health=(seat.effective_health if seat and seat.effective_health is not None else 0),
        tavern_tier=(seat.tech_level if seat and seat.tech_level is not None else 0),
        headline=report.headline or "",
        stale=stale,
        error=error,
        model_note=model_note,
    )

    # 「黑暗之赐」（BG36 机制）：能点了就提醒，已经挂上赐福的随从也列出来。
    gift = getattr(report, "dark_gift", None)
    if gift is not None and (gift.available or gift.granted or gift.known_effects):
        frame.dark_gift = {
            "available": bool(gift.available),
            "button_entity_id": gift.button_entity_id,
            "granted": list(gift.granted),
            # 只发最近 6 个 + 总数：整局能攒近 30 个，全发给面板没地方画
            "known_effects": list(gift.known_effects[-6:]),
            "known_effect_count": len(gift.known_effects),
        }

    # 待选（饰品四选一 / 黑赐三选一）：有就一定要显示 —— 不做决定会卡住回合。
    choice = getattr(report, "pending_choice", None)
    if choice is not None and choice.options:
        # 模型对每个选项的概率（按中文名对齐）。规则层刻意不打分，
        # 这里的是面板上唯一"选哪个"的量化依据。
        ch_probs: Dict[str, float] = {}
        if advice is not None and advice.used_model:
            ch_ans = advice.answer_for("pending_choice")
            if ch_ans is not None:
                for nm, p in choice_ranking(advice.report, ch_ans):
                    ch_probs[nm] = round(float(p), 4)
        frame.pending_choice = {
            "kind": choice.kind,
            "title": choice.display_title,
            "source_name": choice.source_name,
            "source_text": choice.source_text,
            "options": [
                {
                    "index": o.index,
                    "card_id": o.card_id,
                    "name": o.label,
                    "note": o.note,
                    # 黑暗之赐的赐福是**另一张卡**（靠 `DARK_GIFT_ENTITY` 关联）——
                    # 光给随从名看不出强弱，赐福给了什么词条才是关键。
                    "gift_card_id": o.gift_card_id,
                    "gift_name": o.gift_label,
                    "gift_note": o.gift_note,
                    "probability": ch_probs.get(o.label),
                }
                for o in choice.options
            ],
        }

    # 已装备的饰品（小/大）
    for tr in getattr(report, "trinkets", []) or []:
        frame.trinkets.append({
            "size": tr.size,
            "card_id": tr.card_id,
            "name": tr.label,
            "text": tr.text,
        })

    # 本局黑赐记录（给了哪只随从）
    for g in getattr(report, "dark_gift_uses", []) or []:
        frame.dark_gift_uses.append({
            "minion_name": g.minion_name,
            "minion_card_id": g.minion_card_id,
            "minion_entity_id": g.minion_entity_id,
        })

    # 模型对每个槽位的 P(买)。键是槽位实体 id。
    buy_probs: Dict[int, float] = {}
    if advice is not None and advice.used_model:
        buy_probs = advice.buy_predictions()
        # ⚠️ `pending_choice` 必须排在最前：它是当前唯一会卡住回合的决策，
        # 而规则层对饰品/赐福刻意不打分，模型概率是唯一的信息来源。
        for key in ("pending_choice", "pick_shop", "level_up"):
            answer = advice.answer_for(key)
            if answer is None:
                continue
            frame.model_answers.append({
                "question": question_text(key, advice.report),
                "answer": answer_text(key, answer, advice.report, st.shop),
                "probability": round(float(answer.confidence or 0.0), 4),
                "seconds": round(float(answer.seconds or 0.0), 3),
            })

    # 逐张显示模型意见 —— 放在每行商店下方比单独一段更好扫。
    # ⚠️ 展示顺序按 `slot_entity_id` 排（= 炉石商店里从左到右的槽位顺序），
    # 而 `report.shop` 是**按分数排序**的。两套编号混用会让"商店第 N 项"
    # 指向另一张卡（实测踩过：模型答 slot 1，翻出来是排第 5 的那张）。
    # 所以这里统一以槽位顺序为准，另外给一个 `rank` 表示规则引擎的排序。
    rank_by_slot = {item.slot_entity_id: i
                    for i, item in enumerate(report.shop, start=1)}
    for idx, item in enumerate(
            sorted(report.shop, key=lambda it: it.slot_entity_id), start=1):
        prob = buy_probs.get(item.slot_entity_id)
        frame.shop.append({
            "display_index": idx,
            "rank": rank_by_slot.get(item.slot_entity_id, 0),
            "index": item.index,
            "slot_entity_id": item.slot_entity_id,
            "name": item.name,
            "attack": item.attack,
            "health": item.health,
            "tech_level": item.tech_level or 0,
            "golden": bool(item.golden),
            "score": round(float(item.score or 0.0), 2),
            "action": item.action,
            "reasons": list(item.reasons),
            "keywords": list(item.keywords),
            "model_buy_probability": (round(float(prob), 4) if prob is not None else None),
        })

    for e in st.board:
        frame.board.append(_minion_row(e, resolver))

    for e in st.hand:
        frame.hand.append(resolver.name_of(e.card_id) if resolver else (e.card_id or "?"))

    for s in st.opponents:
        frame.opponents.append({
            "player_id": s.player_id,
            "hero_name": (resolver.name_of(s.hero_card_id) if resolver and s.hero_card_id else ""),
            "health": s.effective_health if s.effective_health is not None else 0,
            "tavern_tier": s.tech_level or 0,
            "is_dead": bool(s.effective_health == 0),
        })

    for w in report.warnings:
        frame.warnings.append({"level": w.level, "text": w.text})

    if report.level is not None:
        lv = report.level
        frame.level_advice = {
            "cost": lv.cost or 0,
            "affordable": bool(lv.affordable),
            "current_tier": lv.current_tier or 0,
            "next_tier": lv.next_tier or 0,
            "action": lv.action,
            "reasons": list(lv.reasons),
        }

    return frame
