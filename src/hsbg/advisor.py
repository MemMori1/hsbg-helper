#!/usr/bin/env python3
"""P4：把盘面交给 JevK5（模型顾问层），与规则引擎的建议并排输出。

设计约束（需求文档 ADR-2 / ADR-4）
----------------------------------
- **ADR-2 规则优先、模型兜底**：`RulesEngine` 出硬建议（数字、费用、血线），
  JevK5 只在规则给不出区分度的地方补一刀（买这里面的哪一张、要不要升本）。
  模型不可用时**整条链路照常工作**，只是少了"模型意见"那几行。
- **ADR-4 盘面统一转英文**：JevK5 是 English-only，`evidence` 必须英文。
  这里的 `build_evidence()` 就是那层翻译，绝不把中文喂进去。

本模块不含任何执行接口（§2.3 红线）。
"""
from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .cards import CardDB, CardResolver
from .provider import (Advisor, Answer, JevK5Advisor, ModelUnavailable,
                       Question)
from .rules import (AdviceReport, RulesEngine, clean_mechanics,
                    evaluate_state)
from .state import Entity, GameState, Seat, ShopSlot


# ---------------------------------------------------------------------------
# 盘面 → 英文 evidence
# ---------------------------------------------------------------------------

def _num(v: Optional[int], dash: str = "?") -> str:
    return dash if v is None else str(v)


def _race_en(db: Optional[CardDB], card_id: str) -> str:
    """单个随从的种族英文（喂模型的文本，不能是中文）。"""
    if db is None or not card_id:
        return ""
    card = db.get(card_id)
    if card is None:
        return ""
    races = card.races or (() if not card.race else (card.race,))
    return "/".join(races)


def minion_en(name: str, attack: Optional[int], health: Optional[int],
              tech_level: Optional[int] = None, golden: bool = False,
              mechanics: Sequence[str] = (), race: str = "") -> str:
    """一个随从的英文描述，如 `Golden Sporeling (Beast, tier 2, 2/2, TAUNT)`。"""
    bits: List[str] = []
    if race:
        bits.append(race)
    if tech_level is not None:
        bits.append("tier %d" % tech_level)
    bits.append("%s/%s" % (_num(attack), _num(health)))
    if mechanics:
        bits.append(",".join(mechanics))
    tag = "Golden " if golden else ""
    return "%s%s (%s)" % (tag, name, ", ".join(bits))


def _entity_line(db: Optional[CardDB], resolver: Optional[CardResolver],
                 e: Entity, pos: Optional[int] = None,
                 db_en: Optional[CardDB] = None) -> str:
    """场面/手牌上的一张牌。名字优先取**英文**卡表名（ADR-4：evidence 全英文）。"""
    card_id = e.card_id
    name = ""
    mechanics: Sequence[str] = ()
    if db is not None and card_id:
        card = db.get(card_id)
        if card is not None:
            mechanics = clean_mechanics(card.mechanics)
    # ⚠️ 名字走英文卡表（靠 CardID，语言无关）；中文名会让 2B 模型读不懂。
    if db_en is not None and card_id:
        card_en = db_en.get(card_id)
        if card_en is not None:
            name = card_en.name
    if not name and db is not None and card_id:
        card = db.get(card_id)
        if card is not None:
            name = card.name
    if not name:
        name = e.name or card_id or ("entity %d" % e.entity_id)
    line = minion_en(name, e.attack, e.effective_health if hasattr(e, "effective_health")
                     else e.health, e.tech_level, e.golden, mechanics,
                     _race_en(db, card_id))
    if pos is not None:
        line = "%d. %s" % (pos, line)
    return line


def _flatten(text: str) -> str:
    """卡文压成一行喂模型：去 HTML 标记、空白压平。

    ⚠️ 卡文里有 `<b>` 和换行（实测 `伦德尔标签`、`死亡护命匣` 都是折行的），
    不压平会往 evidence 里塞进换行，把"每项一行"的格式搞坏。
    """
    plain = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", plain).strip()


def _seat_health(s: Seat) -> int:
    return s.effective_health if s.effective_health is not None else -1


def build_evidence(st: GameState, db: Optional[CardDB] = None,
                   resolver: Optional[CardResolver] = None,
                   report: Optional[AdviceReport] = None,
                   db_en: Optional[CardDB] = None) -> str:
    """把盘面写成给 JevK5 读的英文 `evidence` 文本。

    刻意保持紧凑（JevK5 输入上限 16,384 token，且短文本判断更稳）。
    """
    lines: List[str] = []
    seat = st.local_seat
    lines.append("Hearthstone Battlegrounds, turn %d, phase %s%s."
                 % (st.turn,
                    st.step or "UNKNOWN",
                    "" if st.in_recruit_phase else " (not the recruit phase)"))
    gold = seat.gold if seat else None
    lines.append("Me: %s, %s gold, tavern tier %s, %s health."
                 % ("player %d" % seat.player_id if seat else "unknown",
                    _num(gold), _num(seat.tech_level if seat else None),
                    _num(_seat_health(seat) if seat else None)))
    if report is not None:
        lines.append("Next tavern upgrade costs %s gold."
                     % _num(report.level.cost if report.level else None))

    # ⚠️ 序号必须和规则引擎 `ShopItem.index` 一致：规则按**游戏内槽位顺序**编号、
    # 再按分数排序输出，所以这里要从 report 反查 slot→index，而不是用枚举序号——
    # 否则问题里的 "shop slot N" 与 evidence 列表里的第 N 行不是同一张卡。
    num_by_slot: Dict[int, int] = {}
    if report is not None:
        for it in report.shop:
            num_by_slot[it.slot_entity_id] = it.index
    shop = [s for s in st.shop if s.belongs_to_local]
    live = [s for s in shop if not s.empty]
    lines.append("Shop (%d minions, numbered as in the advice panel):" % len(live))
    for seq, s in enumerate(live, 1):
        card = db.get(s.card_id) if (db is not None and s.card_id) else None
        card_en = (db_en.get(s.card_id)
                   if (db_en is not None and s.card_id) else None)
        name = ((card_en.name if card_en is not None else "")
                or (card.name if card is not None else "")
                or s.name or s.card_id)
        # ⚠️ 必须过 `clean_mechanics`：卡表原始 mechanics 里 `TRIGGER_VISUAL`
        # 占 24%（1,410/5,787），直接喂给模型是纯噪声，且与规则层口径不一致。
        mechanics = clean_mechanics(card.mechanics) if card is not None else ()
        lines.append("  %d. %s" % (num_by_slot.get(s.slot_entity_id, seq),
                                   minion_en(
                                       name, s.attack, s.health, s.tech_level,
                                       s.golden, mechanics,
                                       _race_en(db, s.card_id))))

    # 酒馆法术：和随从分开列 —— 它们没有攻/血，买价也不是 3。
    # 不写进去的话模型看不到商店里那张 1 金法术，等于让它漏掉一个选项。
    spells = [s for s in st.spells if s.belongs_to_local and not s.empty]
    if spells:
        lines.append("Tavern spells (%d, buyable this turn):" % len(spells))
        for i, s in enumerate(spells, 1):
            card = db.get(s.card_id) if (db is not None and s.card_id) else None
            card_en = (db_en.get(s.card_id)
                       if (db_en is not None and s.card_id) else None)
            name = ((card_en.name if card_en is not None else "")
                    or (card.name if card is not None else "")
                    or s.name or s.card_id)
            cost = s.cost
            if cost is None and card is not None:
                cost = card.cost
            text = _flatten((card_en.text if card_en is not None else "")
                            or (card.text if card is not None else ""))
            line = "  %d. %s (%s gold)" % (i, name, cost if cost is not None else "?")
            if text:
                line += " — " + text
            lines.append(line)

    lines.append("My board (%d minions):" % len(st.board))
    for i, e in enumerate(st.board, 1):
        lines.append("  " + _entity_line(db, resolver, e, i, db_en=db_en))
    if st.hand:
        lines.append("My hand (%d cards):" % len(st.hand))
        for i, e in enumerate(st.hand, 1):
            lines.append("  " + _entity_line(db, resolver, e, i, db_en=db_en))

    # 已装备的饰品（小/大）—— 决定打法方向，模型必须知道。
    trinkets = list(getattr(st, "trinkets", []) or [])
    if trinkets:
        bits = []
        for tr in trinkets:
            card_en = (db_en.get(tr.card_id)
                       if (db_en is not None and tr.card_id) else None)
            name = ((card_en.name if card_en is not None else "")
                    or tr.name or tr.card_id)
            size = "Small" if tr.size == "small" else "Large"
            text = card_en.text if card_en is not None else ""
            bits.append("%s Trinket: %s%s"
                        % (size, name, (" — " + _flatten(text)) if text else ""))
        lines.append("My trinkets: " + " | ".join(bits) + ".")

    # 当前等待玩家做的选择（饰品四选一 / 黑赐三选一 / 三连奖励…）。
    # ⚠️ 不给模型看选项就让它推概率 = 瞎猜；这是 `choice` 题的题面来源。
    pc = getattr(report, "pending_choice", None) if report is not None else None
    if pc is not None and pc.options:
        title_en = getattr(pc, "title_en", "") or pc.display_title
        lines.append("%s — choose exactly one:" % title_en)
        src_text_en = getattr(pc, "source_text_en", "") or pc.source_text
        if src_text_en:
            lines.append("  (%s)" % _flatten(src_text_en))
        for o in pc.options:
            # ⚠️ 必须用 `label_en`/`note_en`（英文卡表），不能用中文 `label`：
            # ADR-4 要求题面与 evidence 全英文，而中文卡名会让 2B 模型直接懵。
            note = (o.note_en or "")
            lines.append("  %d. %s%s" % (o.index + 1, o.label_en,
                                         (" — " + note) if note else ""))

    # 三连进度：只显示"已经在攒"的那些对子，避免把整个场面重复一遍。
    if report is not None:
        for t3 in report.triples[:3]:
            if not t3.in_shop and t3.owned > 0:
                nm_en = t3.name
                c_en = (db_en.get(t3.card_id)
                        if (db_en is not None and t3.card_id) else None)
                if c_en is not None and c_en.name:
                    nm_en = c_en.name
                # ⚠️ `needed` 是"还差几张"（= 3 - owned），直接拿去当分母会写出
                # `2/1` 这种荒唐数字（实测踩过）。三连固定要 3 张。
                lines.append("Triple progress: %d/%d copies of %s on my board."
                             % (t3.owned, t3.owned + t3.needed, nm_en))

    opps = sorted(st.opponents, key=_seat_health)
    if opps:
        lines.append("Opponents' health: " + ", ".join(
            "player %d %d" % (o.player_id, _seat_health(o)) for o in opps) + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 类型化提问
# ---------------------------------------------------------------------------

def buy_questions(st: GameState, report: AdviceReport,
                  limit: int = 12) -> List[Question]:
    """对每张商店随从问一个买/不买（`noul`）。

    ⚠️ 题面只用英文（ADR-4）；具体是哪张随从由 `evidence` 里的商店列表提供，
    这里只引用槽位序号。
    """
    out: List[Question] = []
    for item in report.shop[:limit]:
        out.append(Question(
            qtype="noul",
            instructions=(
                "The minion in shop slot %d costs 3 gold to buy. Given my board, "
                "my gold, and my tavern tier, is buying that minion the right play now?"
                % item.index),
            # ⚠️ key 用**槽位实体 id**（唯一），不要用 `item.index`（只在一帧内唯一，
            # 而且用户界面用的是 1..N 的显示序号，两者容易混）。
            key="buy_slot_%d" % item.slot_entity_id))
    return out


def level_question(st: GameState, report: AdviceReport) -> Optional[Question]:
    """要不要升本（`noul`）。规则引擎给不出"几本该升"的绝对答案，适合模型补。"""
    lv = report.level
    if lv is None or lv.next_tier is None:
        return None
    return Question(
        qtype="noul",
        instructions=(
            "Upgrading my tavern from tier %s to tier %s costs %s gold%s. "
            "Is upgrading now the right play?"
            % (_num(lv.current_tier), lv.next_tier, _num(lv.cost),
               "" if (lv.cost is not None and report.gold is not None
                      and report.gold >= lv.cost) else " but I cannot afford it yet")),
        key="level_up")


def pick_question(st: GameState, report: AdviceReport,
                  limit: int = 12) -> Optional[Question]:
    """如果只能买一张，买哪张（`choice`）。选项上限 16。"""
    # ⚠️ 必须按 `slot_entity_id`（= 商店槽位从左到右的顺序）排，与 evidence 里
    # `Shop (N)` 那一段完全一致。`report.shop` 是按分数排序的，直接用它的顺序
    # 会出现"选项 1 对应第 5 行"的错位（实测踩过）。
    items = sorted(report.shop, key=lambda it: it.slot_entity_id)[:limit]
    if len(items) < 2:
        return None
    # ⚠️ 选项文本必须是**英文**（ADR-4）：`it.name` 是中文卡名，喂进去等于让模型
    # 在英文语境里读中文。
    # ⚠️ 编号是**盘面显示序号**（1..N），不是 `it.index`（游戏内槽位实体 id，
    # 刷新时越建越大，会显示成"商店第 17 项"这种荒谬编号）。
    crit = ["Buy the minion in shop slot %d" % i for i in range(1, len(items) + 1)]
    crit.append("Buy nothing, keep my gold")
    return Question(qtype="choice", instructions=(
        "If I buy exactly one minion from the shop this turn, which one improves "
        "my board the most?"), criteria=crit, key="pick_shop")


def choice_question(st: GameState, report: AdviceReport) -> Optional[Question]:
    """当前待选做成 `choice` 题（饰品四选一 / 黑赐三选一 / 三连奖励 / 卡牌效果发现）。

    ⚠️ 这块**只能**这样问：规则层对饰品与赐福刻意不打分（420 张效果差异没有样本），
    而"该选哪个"正是 JevK5 最擅长的相对判断 —— 给全选项 + 盘面上下文让它推概率。

    选项 id 用 **1..N 的显示序号**（与 evidence 里 `1. ...` 列表一致），
    这样返回的 `Answer.choice` 直接就是序号，翻译回中文名不会错位。
    """
    pc = report.pending_choice
    if pc is None or len(pc.options) < 2:
        return None
    # ⚠️ 选项文本带上 "N. " 前缀：`pick_question` 立的契约是"裸文本即选项 id"，
    # 所以这里靠**数字前缀**把序号带出去，翻译层再把它换回中文卡名。
    opts: List[str] = []
    for i, o in enumerate(pc.options, 1):
        note = o.note_en or ""
        opts.append("%d. %s%s" % (i, o.label_en,
                                  (" — " + note) if note else ""))
    title_en = getattr(pc, "title_en", "") or pc.display_title
    return Question(
        qtype="choice",
        instructions=(
            "%s. Given my board, my trinkets, my health and my tavern tier, "
            "which one of these options is the strongest pick for me right now?"
            % title_en),
        criteria=opts,
        key="pending_choice")


def choice_options(report: AdviceReport) -> List[str]:
    """待选项的中文展示名（与 `choice_question` 的 1..N 序号一一对应）。"""
    pc = report.pending_choice
    if pc is None:
        return []
    return [o.label for o in pc.options]


def build_questions(st: GameState, report: AdviceReport,
                    limit: int = 12) -> List[Question]:
    """一次咨询要问的全部问题（顺序稳定，便于并排打印）。

    ⚠️ 待选（有待选时）必须**排在最前**：它是当前唯一阻塞玩家的决策，
    比"买哪张"更该先看到模型的意见。
    """
    qs: List[Question] = []
    ch = choice_question(st, report)
    if ch is not None:
        qs.append(ch)
    pick = pick_question(st, report, limit=limit)
    if pick is not None:
        qs.append(pick)
    lv = level_question(st, report)
    if lv is not None:
        qs.append(lv)
    qs.extend(buy_questions(st, report, limit=limit))
    return qs


# ---------------------------------------------------------------------------
# 咨询结果
# ---------------------------------------------------------------------------

def slot_label(shop: Sequence[ShopSlot], slot_entity_id: int,
               report: Optional[AdviceReport] = None) -> str:
    """把槽位号翻译成用户看得懂的说法：`"无面行动者（商店第 5 项）"`。

    ⚠️ 规则引擎的 `ShopItem.index` 是**游戏内槽位号**（刷新时按顺序建槽，会越来越大），
    和盘面上 1..N 的显示序号**不是一回事**。直接给用户看 `商店 #5` 会让人去找第 5 条，
    而商店只有 4 条。所以这里落到"哪张卡 + 显示第几项"。
    """
    num = None
    if report is not None:
        for it in report.shop:
            if it.slot_entity_id == slot_entity_id:
                num = it.index
                break
    live = [s for s in shop if not s.empty]
    disp = None
    name = ""
    for i, s in enumerate(live, 1):
        if s.slot_entity_id == slot_entity_id:
            disp = i
            name = (report_name(report, slot_entity_id)
                    or s.name or s.card_id or ("entity %d" % (s.minion_entity_id or 0)))
            break
    if disp is None:
        return "商店 #%s" % (num if num is not None else slot_entity_id)
    return "%s（商店第 %d 项）" % (name, disp)


def report_name(report: Optional[AdviceReport],
                slot_entity_id: int) -> str:
    if report is None:
        return ""
    for it in report.shop:
        if it.slot_entity_id == slot_entity_id:
            return it.name
    return ""


@dataclass
class Advice:
    """`consult()` 的完整结果：规则部分永远有，模型部分可能为空。"""
    report: AdviceReport
    evidence: str = ""
    answers: List[Answer] = field(default_factory=list)
    provider: str = "rules-only"
    note: str = ""

    @property
    def used_model(self) -> bool:
        return bool(self.answers)

    def answer_for(self, key: str) -> Optional[Answer]:
        for a in self.answers:
            if a.key == key:
                return a
        return None

    def buy_predictions(self) -> Dict[int, float]:
        """{商店槽位实体 id: P(买)}。

        用**槽位实体 id** 做键（而不是规则里的显示序号），这样调用方能用
        `slot_label()` 把它翻回"哪张卡 + 商店第几项"。
        """
        out: Dict[int, float] = {}
        for a in self.answers:
            if a.key.startswith("buy_slot_"):
                slot = a.key[len("buy_slot_"):]
                if slot.isdigit():
                    out[int(slot)] = a.noul or 0.0
        return out


def consult(st: GameState, db: Optional[CardDB] = None,
            resolver: Optional[CardResolver] = None,
            advisor: Optional[Advisor] = None,
            engine: Optional[RulesEngine] = None,
            limit: int = 12,
            db_en: Optional[CardDB] = None) -> Advice:
    """规则 + 模型的总入口。

    ⚠️ 模型不可用时不抛异常：返回 `provider="rules-only"` 并在 `note` 里说明，
    调用方（CLI / 将来的悬浮框）照常显示规则建议。

    ⚠️ `db` 省略时会从 `resolver` 上取（`resolver.db`）。不这样做的话，只传
    `resolver` 的调用方（`panel.py` 的 `ModelAdvisorRunner`）会把 evidence 退化成
    裸 CardID —— 模型看到的是 `BG36_506 (tier 4, 5/2)` 而不是 `复映无人机 ...`，
    同一帧问出不同答案（实测：带卡表答 slot 2，不带卡表答 slot 1）。
    """
    if db is None and resolver is not None:
        db = getattr(resolver, "db", None)
    if db_en is None and resolver is not None:
        db_en = getattr(resolver, "db_en", None)
    rep = (engine or RulesEngine(db, db_en=db_en)).evaluate(st)
    adv = Advice(report=rep)
    adv.evidence = build_evidence(st, db=db, resolver=resolver, report=rep,
                                  db_en=db_en)
    if advisor is None:
        return adv
    try:
        if not advisor.available():
            adv.note = "模型服务不可用（%s）" % advisor.name
            return adv
        questions = build_questions(st, rep, limit=limit)
        adv.answers = [advisor.decide(adv.evidence, q) for q in questions]
        adv.provider = advisor.name
    except ModelUnavailable as e:
        adv.note = "模型调用失败：%s" % e
        adv.answers = []
    return adv


def question_text(key: str, report: Optional[AdviceReport] = None) -> str:
    """把提问的 key 翻成给用户看的短标签。

    `buy_slot_<id>` 这类 key 对着 `report.shop` 反查名字，翻不出来就退回"商店某张"，
    绝不把裸实体 id 丢给用户。
    """
    if key == "level_up":
        return "现在升本？"
    if key == "pick_shop":
        return "只买一张的话"
    if key == "pending_choice":
        return "这个四选一/三选一该选哪个"
    if key.startswith("buy_slot_"):
        slot = key[len("buy_slot_"):]
        name = report_name(report, int(slot)) if slot.isdigit() else ""
        return "买「%s」？" % name if name else "买这张？"
    return key


def answer_text(key: str, answer: "Answer",
                report: Optional[AdviceReport] = None,
                shop: Optional[Sequence[ShopSlot]] = None) -> str:
    """把模型的答案翻成中文短语。

    ⚠️ 两类答案的语义方向相反/不同，不能共用一套文案：
      - `buy_slot_*`：`P(true)` 是"该买"，取 `noul`（p(true)）
      - `level_up`：`P(true)` 也是"该升"，同样取 `noul`，但文案是"升/不升"
      - `pick_shop`：`choice`，答案**是英文选项原文**（"Buy the minion in shop slot 2"），
        必须翻回中文卡名，否则面板上会直接冒一句英文。
    """
    if answer.qtype == "choice":
        raw = str(answer.choice or "")
        if key == "pick_shop":
            return _pick_shop_text(raw, report, shop)
        if key == "pending_choice":
            return _pending_choice_text(raw, report)
        return raw or "?"
    if answer.qtype == "noul":
        p_true = float(answer.noul or 0.0)
        yes = p_true >= 0.5
        if key == "level_up":
            return "升" if yes else "不升"
        if key.startswith("buy_slot_"):
            return "买" if yes else "不买"
        return "是" if yes else "否"
    if answer.qtype == "score":
        return str(answer.score if answer.score is not None else "?")
    return str(answer.choice or "")


def _pending_choice_text(raw: str, report: Optional[AdviceReport]) -> str:
    """把待选题返回的选项序号翻成中文名。

    ⚠️ `choice_question` 的选项 id 就是 **1..N 的显示序号**，所以这里按下标取
    `report.pending_choice.options` 即可，不涉及槽位那套编号。
    取不到就把英文原名原样返回（宁可见英文，也不要显示错卡名）。
    """
    if report is None:
        return raw or "?"
    pc = report.pending_choice
    if pc is None:
        return raw or "?"
    text = (raw or "").strip()
    # ⚠️ 选项文本形如 `"2. Amplifier — ..."`，直接找纯数字 token 是找不到的
    # （`"2."` 带点，`str.isdigit()` 为假）。先剥掉前导序号。
    num = None
    m = re.match(r"^\s*(\d+)\s*[.)\u3001]?\s*", text)
    if m:
        num = int(m.group(1))
        text = text[m.end():]
    if num is None:
        for token in raw.replace(",", " ").split():
            if token.isdigit():
                num = int(token)
                break
    if num is None:
        # 兜底：模型偶尔会把选项文本抄回来但丢了序号，按文本前缀反查。
        low = text.lower()
        for o in pc.options:
            if o.label_en and low.startswith(o.label_en.strip().lower()):
                return o.label
        return raw or "?"
    if 1 <= num <= len(pc.options):
        return pc.options[num - 1].label
    return raw or "?"


def choice_ranking(report: Optional[AdviceReport],
                   answer: "Answer") -> List[Tuple[str, float]]:
    """待选题的概率分布，按概率降序 → `[(中文名, p), ...]`。

    ⚠️ 只有 `choice` 题有分布；`noul` 题的概率是 true/false 两个键，不要走这里。
    名称取不到时退回选项序号，绝不返回空串（面板要按它排版）。
    """
    if report is None or answer is None or not answer.probabilities:
        return []
    pc = report.pending_choice
    out: List[Tuple[str, float]] = []
    for k, v in answer.probabilities.items():
        label = None
        if k.isdigit() and pc is not None and 1 <= int(k) <= len(pc.options):
            label = pc.options[int(k) - 1].label
        elif pc is not None:
            # ⚠️ 真实实现里 `probabilities()` 的键是**选项文本**（`"1: 名字 — 卡文"`），
            # 不是裸序号，所以数字分支几乎不会命中。先剥掉前导序号再按文本前缀反查，
            # 否则面板上会显示一整句英文卡文（实测踩过）。
            low = re.sub(r"^\s*\d+\s*[.)\u3001]?\s*", "", k or "").strip().lower()
            for o in pc.options:
                if o.label_en and low.startswith(o.label_en.strip().lower()):
                    label = o.label
                    break
        out.append((label or ("选项 %s" % k), float(v)))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return out


def _pick_shop_text(raw: str, report: Optional[AdviceReport],
                    shop: Optional[Sequence[ShopSlot]]) -> str:
    """把 `choice` 题型返回的英文选项翻译成 `"买 X（商店第 N 项）"`。

    模型返回的就是我们喂进去的选项原文，形如
    `"Buy the minion in shop slot 2"` 或 `"Buy nothing, keep my gold"`。
    ⚠️ 这里的编号是**盘面显示序号**（1..N），与 `pick_question` 生成的选项一致；
    不要退回去查 `ShopItem.index`（游戏内槽位号），那是另一套编号。
    """
    if "nothing" in raw.lower():
        return "都不买，攒钱"
    num = None
    for token in raw.replace(",", " ").split():
        if token.isdigit():
            num = int(token)
            break
    if num is None or report is None:
        return raw or "?"
    # ⚠️ 必须按 `slot_entity_id` 升序再取第 num 个，和 `pick_question()` 生成选项时
    # 用的是同一个列表。`report.shop` 本身是**按分数排序**的，直接 `report.shop[num-1]`
    # 会取到另一张卡（实测：模型答 slot 2 = 复映无人机，却翻成了排第 1 的无面行动者）。
    ordered = sorted(report.shop, key=lambda it: it.slot_entity_id)
    if 1 <= num <= len(ordered):
        return "买「%s」（商店第 %d 项）" % (ordered[num - 1].name, num)
    return raw or "?"


def advisor_from_args(args) -> Optional[Advisor]:
    """按 CLI 参数造一个 advisor；`--no-model` 或不可用时返回 None。"""
    if getattr(args, "no_model", False):
        return None
    size = getattr(args, "model_size", None)
    if not size:
        # 没显式指定档位时，就从"实际会被 llama-server 加载的那个 GGUF"推出来 ——
        # 否则 4B 模型会套用 2B 的校准温度（1.42，而 4B v0.2 应为 1.532），
        # 概率整体偏平。而"概率偏平"恰恰是 2B 档最被诟病的地方，很容易误判成模型不行。
        try:
            from .model_server import find_gguf, size_key
            size = size_key(find_gguf() or "")
        except Exception:
            size = ""
    return JevK5Advisor(
        url=getattr(args, "model_url", None) or "http://127.0.0.1:8090",
        size=size or "2b",
        timeout=getattr(args, "model_timeout", 60.0) or 60.0)
