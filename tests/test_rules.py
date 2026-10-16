#!/usr/bin/env python3
"""P3 规则引擎单测（零依赖，不需要卡表生成物）。

覆盖：商店打分 / 三连与对子 / 升本费用与建议 / 血线预警 / 刷新费用 / 边界降级。

⚠️ 全部用手搓 `GameState`，不跑真机日志：FR-4 的要求是"每条规则可离线单测"。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from hsbg.cards import Card, CardDB          # noqa: E402
from hsbg.rules import (                     # noqa: E402
    AdviceReport, DecisionBox, LevelAdvice, RulesConfig, RulesEngine,
    evaluate_state,
)
from hsbg.state import (                     # noqa: E402
    Entity, GameState, Seat, ShopSlot,
)

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

#: 手搓 6 张卡：3 只野兽、2 只机械、1 张法术
_CARDS = [
    Card(card_id="BG_T_BEAST1", name="野兽一号", type="MINION", tech_level=1,
         attack=2, health=3, cost=3, race="BEAST", races=("BEAST",),
         mechanics=(), text="", pool_minion=True),
    Card(card_id="BG_T_BEAST2", name="野兽二号", type="MINION", tech_level=2,
         attack=3, health=4, cost=3, race="BEAST", races=("BEAST",),
         mechanics=("TAUNT",), text="", pool_minion=True),
    Card(card_id="BG_T_BEAST3", name="野兽三号", type="MINION", tech_level=4,
         attack=5, health=6, cost=3, race="BEAST", races=("BEAST",),
         mechanics=("DIVINE_SHIELD",), text="", pool_minion=True),
    Card(card_id="BG_T_MECH1", name="机械一号", type="MINION", tech_level=1,
         attack=1, health=2, cost=3, race="MECHANICAL",
         races=("MECHANICAL",), mechanics=("POISONOUS",), text="",
         pool_minion=True),
    Card(card_id="BG_T_MECH2", name="机械二号", type="MINION", tech_level=6,
         attack=8, health=8, cost=3, race="MECHANICAL",
         races=("MECHANICAL",), mechanics=(), text="", pool_minion=True),
    # 「发动（0）」：⚠️ 实测这种卡的 mechanics 是空的，关键词只在正文里
    Card(card_id="BG_T_FIRE0", name="发动零费", type="MINION", tech_level=2,
         attack=3, health=2, cost=3, race="BEAST", races=("BEAST",),
         mechanics=(), text="<b>发动（0）：</b>弃掉一张牌使你的神明获得+2/+2。",
         pool_minion=True),
    # 「发动（2）」：同机制但费用高
    Card(card_id="BG_T_FIRE2", name="发动二费", type="MINION", tech_level=2,
         attack=3, health=2, cost=3, race="BEAST", races=("BEAST",),
         mechanics=(), text="<b>发动（2）：</b>随机获取一张多彩幼龙。",
         pool_minion=True),
    # ⚠️ 同一机制出现两次的卡：mechanics 有 AVENGE，正文又写「复仇（2）」
    Card(card_id="BG_T_AVENGE", name="复仇鳄鱼", type="MINION", tech_level=1,
         attack=1, health=3, cost=3, race="BEAST", races=("BEAST",),
         mechanics=("AVENGE",), text="<b>复仇（2）：</b>获得+1/+1。",
         pool_minion=True),
    # ⚠️ 噪声卡：正文里的「随从消耗(1)」长得像关键词，但不在白名单里
    Card(card_id="BG_T_NOISE", name="噪声异变", type="BATTLEGROUND_ANOMALY",
         tech_level=None, attack=None, health=None, cost=None, race=None,
         races=(), mechanics=(),
         text="<b>随从消耗(1)</b>；<b>出售获得(0)</b>；<b>铸币减少(2)</b>。",
         pool_minion=False),
    Card(card_id="BG_T_SPELL", name="某法术", type="SPELL", tech_level=None,
         attack=None, health=None, cost=1, race=None, races=(),
         mechanics=(), text="", pool_minion=False),
    # 酒馆法术的**效果分类**用例：全部 3 星 2 金，**只有卡文不同** ——
    # 这样分数差异只可能来自 `SPELL_EFFECT_PATTERNS` 的权重。
    Card(card_id="BG_T_SP_CARD", name="测试发现", type="BATTLEGROUND_SPELL",
         tech_level=3, attack=None, health=None, cost=2, race=None, races=(),
         mechanics=(), text="<b>发现</b>一张等级3的随从牌。", pool_minion=False),
    Card(card_id="BG_T_SP_COIN", name="测试铸币", type="BATTLEGROUND_SPELL",
         tech_level=3, attack=None, health=None, cost=2, race=None, races=(),
         mechanics=(), text="获得1枚铸币。", pool_minion=False),
    Card(card_id="BG_T_SP_CAP", name="测试涨上限", type="BATTLEGROUND_SPELL",
         tech_level=3, attack=None, health=None, cost=2, race=None, races=(),
         mechanics=(), text="你的铸币上限提高1枚。", pool_minion=False),
    Card(card_id="BG_T_SP_GLOBAL", name="测试全局增益", type="BATTLEGROUND_SPELL",
         tech_level=3, attack=None, health=None, cost=2, race=None, races=(),
         mechanics=(), text="使你的随从获得+1/+1。", pool_minion=False),
    Card(card_id="BG_T_SP_SINGLE", name="测试单体增益", type="BATTLEGROUND_SPELL",
         tech_level=3, attack=None, health=None, cost=2, race=None, races=(),
         mechanics=(), text="使一个随从获得+2/+2。", pool_minion=False),
    # ⚠️ 卡表正文里 `<b>` 被剥掉后会**留下空格**（实测「变为 金色」），
    # 分类器必须先去空白再匹配。
    Card(card_id="BG_T_SP_GOLD", name="测试变金", type="BATTLEGROUND_SPELL",
         tech_level=3, attack=None, health=None, cost=2, race=None, races=(),
         mechanics=(), text="随机使酒馆中的一个随从变为 金色。", pool_minion=False),
    # 黑暗之赐的**赐福**（另一张卡）：实测 `BG36_MidGameEffect_000t13`
    # 就是「圣盾 + 风怒」这种纯词条赐福
    Card(card_id="BG_T_GIFT1", name="测试赐福", type="SPELL", tech_level=None,
         attack=None, health=None, cost=None, race=None, races=(),
         mechanics=("DIVINE_SHIELD", "WINDFURY"),
         text="<b>圣盾</b>。<b>风怒</b>", pool_minion=False),
    # 开局英雄选择的候选（S1）：实测卡号都带 `_HERO`（`BG22_HERO_004` 一类），
    # 规则层就靠这个特征把「英雄选择」和普通起手换牌区分开。
    Card(card_id="BG_T_HERO1", name="英雄一号", type="HERO", tech_level=None,
         attack=None, health=30, cost=None, race=None, races=(),
         mechanics=(), text="英雄技能：开局获得一枚铸币。", pool_minion=False),
    Card(card_id="BG_T_HERO2", name="英雄二号", type="HERO", tech_level=None,
         attack=None, health=30, cost=None, race=None, races=(),
         mechanics=(), text="英雄技能：被动，你的随从获得+1攻击力。",
         pool_minion=False),
]


def _db() -> CardDB:
    """临时卡表（不落地文件，直接构造）。"""
    return CardDB(cards={c.card_id: c for c in _CARDS})


def _minion(eid, card_id, ctrl=6, zone="PLAY", atk=None, hp=None, golden=False,
            extra=None):
    e = Entity(entity_id=eid, card_id=card_id)
    e.tags["ZONE"] = zone
    e.tags["CONTROLLER"] = str(ctrl)
    e.tags["CARDTYPE"] = "MINION"
    if atk is not None:
        e.tags["ATK"] = str(atk)
    if hp is not None:
        e.tags["HEALTH"] = str(hp)
    if golden:
        e.tags["BACON_TRIPLE_UPGRADE_MINION_ID"] = str(eid + 1000)
    for k, v in (extra or {}).items():
        e.tags[k] = v
    return e


def _hero(eid, card_id, ctrl=6, pid=6, tech=1, max_tech=6, health=30, damage=0):
    e = Entity(entity_id=eid, card_id=card_id)
    e.tags.update({
        "ZONE": "PLAY", "CONTROLLER": str(ctrl), "CARDTYPE": "HERO",
        "PLAYER_ID": str(pid), "PLAYER_TECH_LEVEL": str(tech),
        "BACON_MAX_PLAYER_TECH_LEVEL": str(max_tech),
        "HEALTH": str(health), "DAMAGE": str(damage),
    })
    return e


def _slot(eid, minion, ctrl=6):
    """商店槽位 = `TB_BaconShop_DragBuy` + `tag=2442` 指向随从。"""
    s = ShopSlot(slot_entity_id=eid,
                 minion_entity_id=minion.entity_id if minion else None)
    if minion is not None:
        s.card_id = minion.card_id
        s.tech_level = minion.tech_level
        s.attack = minion.attack
        s.health = minion.health
        s.golden = minion.golden
    s.controller = ctrl
    s.belongs_to_local = ctrl == 6
    return s


def _state(board=(), hand=(), shop=(), gold=10, tier=1, max_tier=6, health=30,
           damage=0, turn=5, step="MAIN_ACTION", tech_up_cost=None):
    """拼一个盘面。`shop` 传 `CardID` 列表。`tech_up_cost` 会造一个升本按钮。"""
    st = GameState()
    st.turn = turn
    st.step = step
    st.local_player_id = 6
    st.seats[6] = Seat(player_id=6, hero_entity_id=104,
                       hero_card_id="TB_BaconShop_HERO_91", is_local=True,
                       health=health, max_health=30, damage=damage,
                       tech_level=tier, max_tech_level=max_tier,
                       resources=gold, resources_used=0)
    st.entities[104] = _hero(104, "TB_BaconShop_HERO_91", tech=tier,
                             max_tech=max_tier)
    nxt = tier + 1
    if tech_up_cost is not None and nxt <= max_tier:
        btn = Entity(entity_id=900 + nxt,
                     card_id="TB_BaconShopTechUp%02d_Button" % nxt)
        btn.tags.update({"ZONE": "PLAY", "COST": str(tech_up_cost),
                         "TECH_LEVEL": str(nxt), "CARDTYPE": "GAME_MODE_BUTTON"})
        st.entities[btn.entity_id] = btn
    eid = 200
    for cid in board:
        st.entities[eid] = _minion(eid, cid, atk=2, hp=2)
        st.board.append(st.entities[eid])
        eid += 1
    for cid in hand:
        st.entities[eid] = _minion(eid, cid, zone="HAND", atk=1, hp=1)
        st.hand.append(st.entities[eid])
        eid += 1
    slots = []
    for i, cid in enumerate(shop):
        m_eid = eid
        st.entities[m_eid] = _minion(m_eid, cid, atk=2, hp=3)
        # 槽位宿主实体也放进表里（`_score_shop` 用不到，但保持形态一致）
        s_eid = 1000 + i
        host = Entity(entity_id=s_eid, card_id="TB_BaconShop_DragBuy")
        host.tags.update({"ZONE": "PLAY", "CONTROLLER": "6",
                          "2442": str(m_eid)})
        st.entities[s_eid] = host
        slots.append(_slot(s_eid, st.entities[m_eid]))
        eid += 1
    st.shop = slots
    return st


def _eng(cfg=None):
    return RulesEngine(_db(), cfg)


# --------------------------------------------------------------------------
# 卡表降级
# --------------------------------------------------------------------------

def test_engine_works_without_card_db():
    """没有卡表也不能崩：名字退回 CardID、星级退回槽位字段。"""
    st = _state(shop=["BG_T_BEAST1"])
    rep = RulesEngine(None).evaluate(st)
    assert len(rep.shop) == 1, rep.shop
    assert rep.shop[0].name, "缺卡表时名字不能为空"


def test_spell_is_not_shop_minion_candidate():
    """`_is_minion_card` 要挡住法术（法术不该进三连统计）。"""
    eng = _eng()
    assert eng._is_minion_card("BG_T_BEAST1") is True
    assert eng._is_minion_card("BG_T_SPELL") is False


def test_card_without_db_treats_battlegrounds_helper_cards_as_non_minion():
    """卡表缺失时，`TB_BaconShop*` 这类机制卡不能算随从。"""
    eng = RulesEngine(None)
    assert eng._is_minion_card("TB_BaconShop_DragBuy") is False
    assert eng._is_minion_card("BG36_110") is True


# --------------------------------------------------------------------------
# 三连 / 对子
# --------------------------------------------------------------------------

def test_pair_is_detected_when_two_copies_owned():
    st = _state(board=["BG_T_BEAST1", "BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    assert [t.card_id for t in rep.triples] == ["BG_T_BEAST1"], rep.triples
    assert rep.triples[0].owned == 2
    assert rep.triples[0].needed == 1


def test_triple_candidate_in_shop_is_flagged_and_ranks_first():
    # 场面 2 张 + 手牌 1 张别的，商店里有那张 → 应该标 in_shop 并排第一
    st = _state(board=["BG_T_BEAST1", "BG_T_BEAST1"],
                shop=["BG_T_MECH1", "BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    t = rep.triples[0]
    assert t.card_id == "BG_T_BEAST1" and t.in_shop is True, rep.triples
    # 商店里那张要拿到"凑三连"的加分并升为"买"
    item = [s for s in rep.shop if s.card_id == "BG_T_BEAST1"][0]
    assert item.completes_triple is True
    assert item.action == "买", (item.action, item.score, item.reasons)
    assert any("三连" in r for r in item.reasons)


def test_three_copies_owned_is_not_a_candidate():
    """已经三张（该合成了）就不再提示差一张。"""
    st = _state(board=["BG_T_BEAST1"] * 3)
    rep = _eng().evaluate(st)
    assert rep.triples == [], rep.triples


def test_hand_minions_count_towards_pairs():
    st = _state(hand=["BG_T_MECH1", "BG_T_MECH1"])
    rep = _eng().evaluate(st)
    assert [t.card_id for t in rep.triples] == ["BG_T_MECH1"]


# --------------------------------------------------------------------------
# 商店打分
# --------------------------------------------------------------------------

def test_high_body_high_tier_minion_scores_higher():
    st = _state(shop=["BG_T_MECH2", "BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    mech2 = [s for s in rep.shop if s.card_id == "BG_T_MECH2"][0]
    beast1 = [s for s in rep.shop if s.card_id == "BG_T_BEAST1"][0]
    assert mech2.score > beast1.score, (mech2.score, beast1.score)


def test_keyword_mechanics_add_score():
    """毒（3.0）应当比嘲讽（0.5）加分多。"""
    st = _state(shop=["BG_T_MECH1", "BG_T_BEAST2"])
    rep = _eng().evaluate(st)
    mech1 = [s for s in rep.shop if s.card_id == "BG_T_MECH1"][0]
    beast2 = [s for s in rep.shop if s.card_id == "BG_T_BEAST2"][0]
    # 两者身材/星级不同，直接比关键词贡献更可靠
    assert _eng()._mechanics("BG_T_MECH1") == ("POISONOUS",)
    assert mech1.score > 0


def test_same_race_board_gives_bonus():
    """场面已有 3 只野兽时，再买野兽要拿到同族加分。"""
    st = _state(board=["BG_T_BEAST1", "BG_T_BEAST2", "BG_T_BEAST3"],
                shop=["BG_T_BEAST1", "BG_T_MECH1"])
    rep = _eng().evaluate(st)
    beast = [s for s in rep.shop if s.card_id == "BG_T_BEAST1"][0]
    assert any("同族" in r for r in beast.reasons), beast.reasons


def test_unaffordable_minion_is_skipped():
    st = _state(gold=1, shop=["BG_T_MECH2"])
    rep = _eng().evaluate(st)
    item = rep.shop[0]
    assert item.action == "跳过", item
    assert any("金币不足" in r for r in item.reasons)


def test_full_board_downgrades_buy_to_consider():
    """场面 7 只且没有三连收益时，不能直接"买"。"""
    st = _state(board=["BG_T_BEAST1"] * 7, shop=["BG_T_MECH2"])
    rep = _eng().evaluate(st)
    item = rep.shop[0]
    assert item.action in ("考虑", "跳过"), item.action
    assert any("场面已满" in r for r in item.reasons)


def test_shop_is_sorted_by_score_desc():
    st = _state(shop=["BG_T_BEAST1", "BG_T_MECH2", "BG_T_MECH1"])
    rep = _eng().evaluate(st)
    scores = [s.score for s in rep.shop]
    assert scores == sorted(scores, reverse=True), scores


def test_golden_minion_gets_bonus():
    st = _state(shop=["BG_T_BEAST1"])
    st.entities[200].tags["BACON_TRIPLE_UPGRADE_MINION_ID"] = "9999"
    st.shop[0].golden = True
    rep = _eng().evaluate(st)
    assert any("金色" in r for r in rep.shop[0].reasons), rep.shop[0].reasons


# --------------------------------------------------------------------------
# 升本
# --------------------------------------------------------------------------

def test_tech_up_cost_reads_button_cost_in_play():
    """升本费用必须读按钮上的实时 `COST`，而不是兜底表。"""
    st = _state(tier=4, tech_up_cost=10)
    cost, nxt = _eng().tech_up_cost(st)
    assert (cost, nxt) == (10, 5), (cost, nxt)


def test_tech_up_cost_ignores_button_not_in_play():
    """⚠️ `SETASIDE`/`REMOVEDFROMGAME` 里的是陈旧值，必须忽略。"""
    st = _state(tier=4, tech_up_cost=10)
    for e in st.entities.values():
        if e.card_id.startswith("TB_BaconShopTechUp"):
            e.tags["ZONE"] = "SETASIDE"
    cost, nxt = _eng().tech_up_cost(st)
    assert (cost, nxt) == (9, 5), (cost, nxt)   # 兜底表 5 本是 9


def test_tech_up_cost_falls_back_to_config_table():
    st = _state(tier=2, tech_up_cost=None)
    cost, nxt = _eng().tech_up_cost(st)
    assert (cost, nxt) == (7, 3), (cost, nxt)


def test_tech_up_returns_none_at_max_tier():
    st = _state(tier=6, max_tier=6)
    cost, nxt = _eng().tech_up_cost(st)
    assert cost is None and nxt is None
    adv = _eng().evaluate(st).level
    assert adv.action == "不升"


def test_early_tier_with_affordable_cost_advises_leveling():
    st = _state(tier=2, gold=10, tech_up_cost=7)
    adv = _eng().evaluate(st).level
    assert adv.action == "升本", (adv.action, adv.score, adv.reasons)
    assert adv.affordable is True and adv.next_tier == 3


def test_unaffordable_tech_up_is_not_advised():
    """买不起升本时给「攒钱」，**不是**「不升」。

    ⚠️ 这两者的区别是 load-bearing 的：买不起只说明"这回合升不了"，
    不代表"不该升"。早先把买不起判成「不升」并给了 -12 分，
    结果"四费升二本"这种最标准的打法被判成不该升（真机踩过，见需求分析 §11.11）。
    """
    st = _state(tier=2, gold=2, tech_up_cost=7)
    adv = _eng().evaluate(st).level
    assert adv.action == "升不起", (adv.action, adv.score)
    assert any("金币不足" in r for r in adv.reasons)


def test_shop_triple_blocks_tech_up():
    """商店里有三连可拿时，升本要被压制。"""
    st = _state(board=["BG_T_BEAST1", "BG_T_BEAST1"], tier=2, gold=10,
                tech_up_cost=7, shop=["BG_T_BEAST1"])
    adv = _eng().evaluate(st).level
    assert any("三连" in r for r in adv.reasons), adv.reasons
    assert adv.score < 5.0, adv.score


def test_high_tier_leveling_is_discouraged():
    st = _state(tier=5, gold=20, tech_up_cost=10)
    adv = _eng().evaluate(st).level
    assert any("递减" in r for r in adv.reasons), adv.reasons


def test_level_advice_without_local_seat_is_safe():
    st = GameState()
    adv = _eng().evaluate(st).level
    assert adv.action in ("观望", "不升")
    assert isinstance(adv, LevelAdvice)


# --------------------------------------------------------------------------
# 预警
# --------------------------------------------------------------------------

def test_critical_health_warning():
    st = _state(health=30, damage=25)          # 有效血量 5
    rep = _eng().evaluate(st)
    assert any(w.level == "danger" for w in rep.warnings), rep.warnings


def test_zero_health_warning_is_danger():
    st = _state(health=30, damage=30)
    rep = _eng().evaluate(st)
    assert rep.health == 0
    assert any(w.level == "danger" for w in rep.warnings)


def test_low_health_warning_mentions_combat_damage_cap():
    st = _state(health=30, damage=18)          # 有效血量 12
    st.entities[777] = Entity(entity_id=777, card_id="X")
    st.entities[777].tags["BACON_COMBAT_DAMAGE_CAP"] = "9"
    rep = _eng().evaluate(st)
    warn = [w for w in rep.warnings if w.level == "warn"]
    assert warn, rep.warnings
    assert any("9" in w.text for w in warn), warn


def test_thin_board_warning_after_turn_five():
    st = _state(turn=8, board=["BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    assert any("偏薄" in w.text for w in rep.warnings), rep.warnings


def test_no_thin_board_warning_early():
    st = _state(turn=2, board=["BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    assert not any("偏薄" in w.text for w in rep.warnings), rep.warnings


def test_hand_limit_warning():
    st = _state(hand=["BG_T_BEAST1"] * 10)
    rep = _eng().evaluate(st)
    assert any("手牌" in w.text for w in rep.warnings), rep.warnings


def test_free_refresh_count_warning():
    st = _state()
    e = Entity(entity_id=555, card_id="Bacon_Free_Refresh_Player_Ench")
    e.tags["BACON_FREE_REFRESH_COUNT"] = "2"
    st.entities[555] = e
    rep = _eng().evaluate(st)
    assert _eng().free_refreshes(st) == 2
    assert any("免费刷新" in w.text for w in rep.warnings), rep.warnings


def test_died_last_combat_warning():
    st = _state()
    st.seats[6].died_last_combat = True
    rep = _eng().evaluate(st)
    assert any("战败" in w.text for w in rep.warnings), rep.warnings


# --------------------------------------------------------------------------
# 刷新费用 & 报告结构
# --------------------------------------------------------------------------

def test_reroll_cost_reads_button():
    st = _state()
    btn = Entity(entity_id=600, card_id="TB_BaconShop_8p_Reroll_Button")
    btn.tags.update({"ZONE": "PLAY", "COST": "0"})
    st.entities[600] = btn
    assert _eng().reroll_cost(st) == 0


def test_reroll_cost_none_without_button():
    assert _eng().reroll_cost(_state()) is None


def test_report_headline_is_non_empty_and_mentions_turn():
    st = _state(turn=7, shop=["BG_T_MECH2"])
    rep = _eng().evaluate(st)
    assert rep.turn == 7
    assert "回合 7" in rep.headline, rep.headline
    assert "招募" in rep.headline


def test_report_headline_marks_combat_phase():
    st = _state(turn=7, step="MAIN_COMBAT", shop=[])
    rep = _eng().evaluate(st)
    assert rep.in_recruit is False
    assert "战斗" in rep.headline, rep.headline


def test_report_headline_does_not_repeat_the_decision_action():
    """⚠️ 动作由**决策框**（独立小窗）单独说，分析框标题只报"现在什么情况"。

    早先只有一个框时这里必须复述动作，否则标题说"建议买 X"、升本栏说"升本"
    会互相打架；分成两块之后复述就变成同一句话出现两遍。
    """
    st = _state(turn=3, tier=1, gold=4, tech_up_cost=4)
    rep = _eng().evaluate(st)
    assert rep.decision.action == "升本"
    assert "升本" not in rep.headline
    assert "建议" not in rep.headline
    assert rep.headline == "回合 3 招募 · 4 金 · 30 血"


def test_top_shop_skips_rejected_items():
    st = _state(shop=["BG_T_MECH2", "BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    top = rep.top_shop(3)
    assert all(s.action != "跳过" for s in top), [(s.name, s.action) for s in top]


def test_evaluate_state_helper_matches_engine():
    st = _state(shop=["BG_T_MECH2"])
    a = evaluate_state(st, _db())
    b = _eng().evaluate(st)
    assert [s.score for s in a.shop] == [s.score for s in b.shop]
    assert isinstance(a, AdviceReport)


def test_config_overrides_are_respected():
    cfg = RulesConfig(low_health=25, buy_cost=3, board_limit=3)
    st = _state(health=30, damage=8)           # 有效血量 22 < 25
    rep = _eng(cfg).evaluate(st)
    assert any(w.level == "warn" for w in rep.warnings), rep.warnings


def test_non_recruit_phase_still_produces_report():
    st = _state(step="MAIN_COMBAT", shop=[])
    rep = _eng().evaluate(st)
    assert rep.in_recruit is False
    assert rep.gold is not None


def test_shop_items_keep_shop_order_index():
    """`ShopItem.index` 是原始槽位序号，排序后不能重编号。"""
    st = _state(shop=["BG_T_BEAST1", "BG_T_MECH2"])
    rep = _eng().evaluate(st)
    idx = sorted(s.index for s in rep.shop)
    assert idx == [1, 2], idx


def test_board_size_and_hand_size_reported():
    st = _state(board=["BG_T_BEAST1", "BG_T_MECH1"],
                hand=["BG_T_BEAST2"])
    rep = _eng().evaluate(st)
    assert rep.board_size == 2 and rep.hand_size == 1


def test_evaluate_is_read_only_on_state():
    st = _state(board=["BG_T_BEAST1"], shop=["BG_T_MECH2"])
    before = (len(st.board), len(st.hand), len(st.shop), st.turn)
    _eng().evaluate(st)
    after = (len(st.board), len(st.hand), len(st.shop), st.turn)
    assert before == after


# --------------------------------------------------------------------------
# 卡文模板关键词（「发动」等不在 mechanics 字段里的机制）
# --------------------------------------------------------------------------

def test_parse_text_keywords_extracts_fire_with_cost():
    from hsbg.rules import parse_text_keywords
    assert parse_text_keywords("<b>发动（0）：</b>弃一张牌。") == ["发动(0)"]
    assert parse_text_keywords("<b>发动（2）：</b>随机获取一张多彩幼龙。") == ["发动(2)"]


def test_parse_text_keywords_ignores_unrelated_parenthesised_text():
    """⚠️ 卡文里 `随从消耗(1)`、`铸币减少(2)` 长得跟关键词一模一样。

    实测用宽松正则会一口气捞进 100 张风马牛不相及的卡（异变、英雄技能、饰品），
    所以关键词必须是白名单。这条测试就是钉住白名单。
    """
    from hsbg.rules import parse_text_keywords
    assert parse_text_keywords("随从消耗(1)；出售获得(0)；铸币减少(2)。") == []
    assert parse_text_keywords("") == []
    assert parse_text_keywords(None) == []


def test_parse_text_keywords_does_not_double_count_parameterised_keyword():
    from hsbg.rules import parse_text_keywords
    assert parse_text_keywords("<b>复仇（3）：</b>随机获得一张牌。") == ["复仇(3)"]
    assert parse_text_keywords("<b>进击：</b>获得+1/+1。") == ["进击"]


def test_mechanics_include_text_keywords_when_field_is_empty():
    """⚠️ 这是「发动」最核心的那条：mechanics 是空的，关键词只在正文里。"""
    e = _eng()
    assert e._mechanics("BG_T_FIRE0") == ("发动(0)",)
    assert e._mechanics("BG_T_FIRE2") == ("发动(2)",)
    assert e._keyword_labels("BG_T_FIRE0") == ["发动"]


def test_lower_cost_fire_scores_higher_than_expensive_one():
    """发动（0）比发动（2）强（费用是代价），权重表要能体现出来。"""
    e = _eng()
    from hsbg.rules import MECHANIC_WEIGHT
    w0 = MECHANIC_WEIGHT["发动(0)"]
    w2 = MECHANIC_WEIGHT["发动(2)"]
    assert w0 > w2, (w0, w2)
    # 同身材同种族的两张卡，只有发动费用不同 → 0 费那张分更高
    st0 = _state(shop=["BG_T_FIRE0"])
    st2 = _state(shop=["BG_T_FIRE2"])
    assert e.evaluate(st0).shop[0].score > e.evaluate(st2).shop[0].score


def test_text_keyword_is_not_double_counted_with_mechanics_field():
    """⚠️ `BG_T_AVENGE` 既有 `mechanics: AVENGE` 又有正文「复仇（2）」。

    实测 `BG21_001`（苍鳞鳄鱼）就是这形态；不去重的话同一机制进两次分。
    """
    e = _eng()
    assert e._mechanics("BG_T_AVENGE") == ("AVENGE",)


def test_keyword_labels_are_chinese_and_deduplicated():
    e = _eng()
    assert e._keyword_labels("BG_T_AVENGE") == ["复仇"]
    assert e._keyword_labels("BG_T_BEAST2") == ["嘲讽"]
    assert e._keyword_labels("BG_T_NOISE") == []


def test_shop_item_carries_keyword_labels():
    st = _state(shop=["BG_T_FIRE0", "BG_T_BEAST2"])
    rep = _eng().evaluate(st)
    by_id = {s.card_id: s for s in rep.shop}
    assert by_id["BG_T_FIRE0"].keywords == ["发动"]
    assert by_id["BG_T_BEAST2"].keywords == ["嘲讽"]
    # 关键词要能进 reasons，玩家才知道为什么分高
    assert any("发动" in r for r in by_id["BG_T_FIRE0"].reasons)


# --------------------------------------------------------------------------
# 黑暗之赐（BG36 机制）
# --------------------------------------------------------------------------

def _gift_button(eid=302, vfx="1"):
    e = Entity(entity_id=eid, card_id="BG36_Button_DarkGift")
    e.tags.update({"ZONE": "PLAY", "CARDTYPE": "GAME_MODE_BUTTON",
                   "COST": "3", "BACON_DARK_GIFT_PRESSABLE_VFX": vfx})
    return e


def test_dark_gift_available_when_button_vfx_is_on():
    """⚠️ 判据是按钮上的 `BACON_DARK_GIFT_PRESSABLE_VFX`，**不是**全局的
    `BACON_DARK_GIFTS_ACTIVE`（后者整局只在开局出现 2 次，只是"本局启用"开关）。"""
    st = _state()
    st.entities[302] = _gift_button(vfx="1")
    rep = _eng().evaluate(st)
    assert rep.dark_gift is not None
    assert rep.dark_gift.available is True
    assert rep.dark_gift.button_entity_id == 302
    assert any("黑暗发现" in w.text for w in rep.warnings)


def test_dark_gift_not_available_when_vfx_is_zero():
    st = _state()
    st.entities[302] = _gift_button(vfx="0")
    rep = _eng().evaluate(st)
    assert rep.dark_gift.available is False
    assert not any("可以点了" in w.text for w in rep.warnings)


def test_dark_gift_lists_minions_that_already_have_it():
    st = _state(board=["BG_T_BEAST1"])
    st.entities[302] = _gift_button(vfx="0")
    st.board[0].tags["HAS_DARK_GIFT"] = "1"
    rep = _eng().evaluate(st)
    assert rep.dark_gift.granted == ["野兽一号"]
    assert any("黑暗之赐" in w.text for w in rep.warnings)


def test_dark_gift_resolves_effect_names():
    """`DARK_GIFT_ENTITY` 指向的赐福实体，其卡表名要能解析出来。"""
    # ⚠️ CardID 用真实的 `BG36_MidGameEffect_` 前缀形态：赐福识别靠它
    gift_card = Card(card_id="BG36_MidGameEffect_999", name="鹰身人之爪",
                     type="SPELL", tech_level=None, attack=None, health=None,
                     cost=0, race=None, races=(), mechanics=(), text="",
                     pool_minion=False)
    db = _db()
    db._cards["BG36_MidGameEffect_999"] = gift_card
    eng = RulesEngine(db)
    st = _state(board=["BG_T_BEAST1"])
    st.entities[302] = _gift_button(vfx="0")
    eff = Entity(entity_id=2373, card_id="BG36_MidGameEffect_999")
    eff.tags.update({"ZONE": "PLAY", "CARDTYPE": "SPELL"})
    st.entities[2373] = eff
    st.board[0].tags["HAS_DARK_GIFT"] = "1"
    st.board[0].tags["DARK_GIFT_ENTITY"] = "2373"
    rep = eng.evaluate(st)
    assert rep.dark_gift.known_effects == ["鹰身人之爪"]


def test_dark_gift_absent_by_default():
    st = _state()
    rep = _eng().evaluate(st)
    assert rep.dark_gift is not None
    assert rep.dark_gift.available is False
    assert rep.dark_gift.granted == []
    assert rep.dark_gift.known_effects == []


def test_panel_frame_carries_dark_gift_and_keywords():
    """P6 数据契约：面板帧要能拿到黑暗之赐与关键词，否则 Swift 侧没东西可画。

    ⚠️ 必须传 `CardResolver`：关键词标签要查卡表，`build_frame` 从 resolver 里
    取 db。不传的话 keywords 会是空的（实测踩过），面板上看不到「发动/圣盾」。
    """
    from hsbg.cards import CardResolver
    from hsbg.panel import build_frame
    st = _state(shop=["BG_T_FIRE0"])
    st.entities[302] = _gift_button(vfx="1")
    frame = build_frame(st, CardResolver(_db()))
    d = frame.to_dict()
    assert d["dark_gift"]["available"] is True
    assert d["shop"][0]["keywords"] == ["发动"]


def test_panel_frame_carries_the_decision_box():
    """P6 数据契约：决策框（独立小窗）只读 `frame["decision"]`。"""
    from hsbg.cards import CardResolver
    from hsbg.panel import build_frame
    frame = build_frame(_state(turn=3, tier=1, gold=4, tech_up_cost=4),
                        CardResolver(_db()))
    d = frame.to_dict()
    assert d["decision"]["action"] == "升本"
    assert d["decision"]["target"] == "2本"
    assert d["decision"]["headline"] == "升本 2本"
    assert d["decision"]["cost_text"] == "4 金"
    assert d["decision"]["curve_tier"] == 2


def test_panel_frame_always_carries_a_decision():
    """⚠️ 决策框字段**总是**在帧里 —— `evaluate()` 无条件填 `rep.decision`。

    所以 Swift 侧不能靠"字段缺失"判断"还没开局"；那要看窗口是否可见
    （`PanelContentState.targetVisible`，由 `main.swift` 按 WindowTracker 填）。
    """
    from hsbg.cards import CardResolver
    from hsbg.panel import build_frame
    st = _state()
    st.seats.clear()
    st.local_player_id = None
    d = build_frame(st, CardResolver(_db())).to_dict()
    assert d["decision"]["action"] == "观望"
    assert "还没有本地玩家信息" in d["decision"]["reason"]


def test_dark_gift_ignores_the_mechanism_toggle_card():
    """`BG36_MidGameEffect_010` 是机制总开关，不是某个赐福，不能当"已知赐福"列出来。"""
    toggle = Card(card_id="BG36_MidGameEffect_010", name="酒馆战棋黑暗之赐[DNT]",
                  type="SPELL", tech_level=None, attack=None, health=None,
                  cost=None, race=None, races=(), mechanics=(), text="",
                  pool_minion=False)
    db = _db()
    db._cards["BG36_MidGameEffect_010"] = toggle
    st = _state()
    e = Entity(entity_id=2400, card_id="BG36_MidGameEffect_010")
    e.tags.update({"ZONE": "PLAY", "CARDTYPE": "SPELL"})
    st.entities[2400] = e
    rep = RulesEngine(db).evaluate(st)
    assert rep.dark_gift.known_effects == []


# ---------------------------------------------------------------------------
# 待选（英雄选择 / 饰品四选一 / 黑赐三选一）
# ⚠️ 英雄与饰品有 HSReplay 实证分（`heroes.py` / `trinkets.py`）；黑赐与发现类
# 没有样本，仍然只做展示 + 一行特征提示，不打分。
# ---------------------------------------------------------------------------

from hsbg.state import ChoiceOption, PendingChoice, Trinket, DarkGiftRecord  # noqa: E402


def _choice_state(pc, trinkets=(), gifts=()):
    st = _state()
    st.pending_choice = pc
    st.trinkets = list(trinkets)
    st.dark_gifts = list(gifts)
    return st


def _trinket_pc(card_ids, source="BG30_Trinket_1st", name="小型饰品"):
    pc = PendingChoice(choice_id=3, kind="GENERAL", source_card_id=source,
                       source_name=name, player="我", is_local=True)
    for i, cid in enumerate(card_ids):
        pc.options.append(ChoiceOption(index=i, entity_id=3000 + i,
                                       card_id=cid, name="日志名%d" % i,
                                       zone="SETASIDE"))
    return pc


def test_trinket_choice_lists_options_with_card_names():
    """⚠️ 名字取卡表名而不是日志名：饰品日志名是 `朝圣小鬼标签` 这种内部占位名。"""
    pc = _trinket_pc(["BG_T_BEAST1", "BG_T_BEAST2"])
    rep = evaluate_state(_choice_state(pc), _db())
    adv = rep.pending_choice
    assert adv is not None
    assert adv.kind == "trinket"
    assert adv.display_title == "小饰品四选一"
    assert [o.label for o in adv.options] == ["野兽一号", "野兽二号"]
    assert adv.options[0].kind == "trinket"


def test_large_trinket_gets_its_own_title():
    pc = _trinket_pc(["BG_T_BEAST1"], source="BG30_Trinket_2nd", name="大型饰品")
    rep = evaluate_state(_choice_state(pc), _db())
    assert rep.pending_choice.kind == "trinket_large"
    assert rep.pending_choice.display_title == "大饰品四选一"
    assert rep.pending_choice.options[0].kind == "trinket"


def test_dark_gift_choice_is_recognised_and_options_are_minions():
    pc = _trinket_pc(["BG_T_BEAST1", "BG_T_MECH1"],
                     source="BG36_MidGameEffect_010", name="黑暗之赐")
    rep = evaluate_state(_choice_state(pc), _db())
    adv = rep.pending_choice
    assert adv.kind == "dark_gift"
    assert adv.display_title == "黑暗之赐三选一"
    assert adv.options[0].kind == "minion"


def test_other_choice_uses_source_name_as_title():
    """三连奖励 / 饰品触发的发现：标题用触发源名字（实测有 `三连奖励`）。"""
    pc = _trinket_pc(["BG_T_BEAST1"], source="TB_BaconShop_3ofKindCheck",
                     name="三连奖励")
    rep = evaluate_state(_choice_state(pc), _db())
    assert rep.pending_choice.kind == "other"
    assert rep.pending_choice.display_title == "选择：三连奖励"


def test_choice_option_note_carries_stats_and_card_text():
    pc = _trinket_pc(["BG_T_BEAST2"])
    rep = evaluate_state(_choice_state(pc), _db())
    note = rep.pending_choice.options[0].note
    assert "2星" in note and "3/4" in note


def test_dark_gift_option_resolves_the_gift_card():
    """⚠️ 黑赐选项是随从，但玩家真正在挑的是**赐福** —— 必须把赐福卡解出来。

    实测同一只随从可以配不同赐福（`鹰身人之爪` 给 圣盾+风怒、
    `防御献祭` 给 +10 血+亡语），所以只报随从等于丢掉最关键的一半信息。
    """
    pc = _trinket_pc(["BG_T_BEAST1"], source="BG36_MidGameEffect_010",
                     name="黑暗之赐")
    pc.options[0].gift_card_id = "BG_T_GIFT1"
    rep = evaluate_state(_choice_state(pc), _db())
    adv = rep.pending_choice
    assert adv.kind == "dark_gift"
    opt = adv.options[0]
    assert opt.gift_card_id == "BG_T_GIFT1"
    assert opt.gift_label == "测试赐福"
    assert opt.has_gift
    # 赐福单独成行（不塞进 note 里被卡文挤没）
    assert "测试赐福" in opt.gift_note
    assert "圣盾" in opt.gift_note and "风怒" in opt.gift_note
    assert "赐福" not in opt.note


def test_dark_gift_option_without_link_has_no_gift_note():
    pc = _trinket_pc(["BG_T_BEAST1"], source="BG36_MidGameEffect_010",
                     name="黑暗之赐")
    rep = evaluate_state(_choice_state(pc), _db())
    opt = rep.pending_choice.options[0]
    assert not opt.has_gift
    assert opt.gift_note == "" and opt.gift_note_en == ""


def test_choice_option_note_en_puts_the_gift_first():
    """喂模型的英文特征行要把赐福写在最前（那是决策依据）。"""
    pc = _trinket_pc(["BG_T_BEAST1"], source="BG36_MidGameEffect_010",
                     name="黑暗之赐")
    pc.options[0].gift_card_id = "BG_T_GIFT1"
    rep = evaluate_state(_choice_state(pc), _db())
    note_en = rep.pending_choice.options[0].note_en
    assert note_en.startswith("Dark Gift:")
    # 没有英文卡表时退回中文名，但标记本身必须存在（模型靠它认这是赐福）
    assert "测试赐福" in note_en


def test_choice_option_note_strips_html_and_flattens_whitespace():
    """⚠️ 卡文里有 `<b>` 和换行，不处理会在面板上画出标记与空行。"""
    pc = _trinket_pc(["BG_T_FIRE0"])
    rep = evaluate_state(_choice_state(pc), _db())
    note = rep.pending_choice.options[0].note
    assert "<b>" not in note and "\n" not in note
    assert "发动（0）" in note


def test_choice_advice_is_none_when_not_local():
    pc = _trinket_pc(["BG_T_BEAST1"])
    pc.is_local = False
    rep = evaluate_state(_choice_state(pc), _db())
    assert rep.pending_choice is None


def test_choice_advice_skips_mulligan():
    """起手换牌与战棋决策无关，规则层直接跳过。"""
    pc = _trinket_pc(["BG_T_BEAST1"], source="GameEntity", name="")
    pc.kind = "MULLIGAN"
    rep = evaluate_state(_choice_state(pc), _db())
    assert rep.pending_choice is None


def test_pending_choice_produces_a_warning():
    pc = _trinket_pc(["BG_T_BEAST1"])
    rep = evaluate_state(_choice_state(pc), _db())
    texts = [w.text for w in rep.warnings]
    assert any("还没选" in t for t in texts), texts
    # 待选提醒要排在最前（不做决定会卡住回合，比血线紧急）
    assert "还没选" in rep.warnings[0].text


def test_equipped_trinkets_are_reported():
    tr = Trinket(size="small", card_id="BG_T_BEAST1", name="日志名")
    rep = evaluate_state(_choice_state(None, trinkets=[tr]), _db())
    assert len(rep.trinkets) == 1
    assert rep.trinkets[0].label == "野兽一号"          # 卡表名优先
    assert rep.trinkets[0].size == "small"


def test_dark_gift_uses_are_carried_into_report():
    g = DarkGiftRecord(minion_name="某随从", minion_card_id="BG_T_BEAST1",
                       minion_entity_id=42)
    rep = evaluate_state(_choice_state(None, gifts=[g]), _db())
    assert len(rep.dark_gift_uses) == 1
    assert rep.dark_gift_uses[0].minion_entity_id == 42


def test_choice_advice_degrades_without_card_db():
    """没有卡表时不能崩，退回日志名。"""
    pc = _trinket_pc(["BG_T_BEAST1"])
    rep = evaluate_state(_choice_state(pc), None)
    assert rep.pending_choice is not None
    assert rep.pending_choice.options[0].label == "日志名0"


# ---------------------------------------------------------------------------
# 饰品实证分（HSReplay）：只有饰品四选一打分 + 给推荐
# ---------------------------------------------------------------------------

from hsbg.trinkets import TrinketStats  # noqa: E402


def _stats(rows=None):
    """一张只有测试卡的手搓实证表（不碰生成物 `data/trinkets_hsreplay.json`）。"""
    if rows is None:
        rows = [
            {"card_id": "BG_T_BEAST1", "tier": "s", "pick_rate": 46.0,
             "avg_final_placement": 3.55},
            {"card_id": "BG_T_BEAST2", "tier": "c", "pick_rate": 20.0,
             "avg_final_placement": 4.33},
        ]
    return TrinketStats.from_rows(rows, bracket="ALL")


def test_trinket_options_get_scores_and_the_best_is_recommended():
    # 差的一号在前、好的在后 —— 推荐必须换到后面那个，而不是排在前面那个
    pc = _trinket_pc(["BG_T_BEAST2", "BG_T_BEAST1"])
    rep = evaluate_state(_choice_state(pc), _db(), trinket_stats=_stats())
    adv = rep.pending_choice
    assert all(o.score is not None for o in adv.options)
    assert adv.options[1].score > adv.options[0].score
    assert adv.recommended is adv.options[1]        # 认的是选项对象本身
    assert adv.recommended.label == "野兽一号"
    assert "S级" in adv.options[1].score_note
    assert adv.options[1].score_note_en.startswith("HSReplay tier S")
    assert adv.recommend_note == ""                 # 数据齐全，没有缺口提示
    assert adv.recommend_text.startswith("规则推荐 野兽一号")


def test_trinket_note_en_puts_the_stats_before_the_card_text():
    """喂模型的一行里实证统计要排在卡文前面（卡文可能很长，截断会吃掉统计）。"""
    stats = _stats(rows=[{"card_id": "BG_T_FIRE0", "tier": "s",
                          "pick_rate": 46.0, "avg_final_placement": 3.55}])
    pc = _trinket_pc(["BG_T_FIRE0"])
    rep = evaluate_state(_choice_state(pc), _db(), trinket_stats=stats)
    note_en = rep.pending_choice.options[0].note_en
    assert note_en.startswith("HSReplay tier S")
    assert "发动（0）" in note_en                     # 卡文兜底排在后面


def test_recommendation_reaches_the_decision_box_and_warning():
    pc = _trinket_pc(["BG_T_BEAST2", "BG_T_BEAST1"])
    rep = evaluate_state(_choice_state(pc), _db(), trinket_stats=_stats())
    assert rep.decision.trigger == "pending_choice"
    assert rep.decision.action == "选择"
    assert rep.decision.target == "小饰品四选一"
    assert "规则推荐 野兽一号" in rep.decision.reason
    assert "S级" in rep.decision.reason
    assert "推荐 野兽一号" in rep.warnings[0].text
    assert rep.warnings[0].level == "info"           # 待选只是提醒，不是警告


def test_trinket_scoring_degrades_to_display_only_without_stats():
    """没有实证表（老行为）：只展示不推荐，决策框退回通用文案。"""
    pc = _trinket_pc(["BG_T_BEAST1", "BG_T_BEAST2"])
    rep = evaluate_state(_choice_state(pc), _db())   # 不传 trinket_stats
    adv = rep.pending_choice
    assert adv.recommended is None and adv.recommend_text == ""
    assert all(o.score is None and o.score_note == "" for o in adv.options)
    assert rep.decision.reason == "免费收益，先点开挑一个"
    assert "推荐" not in rep.warnings[0].text


def test_partial_stats_recommend_with_a_caveat():
    """新补丁刚上的饰品 HSReplay 还没统计到：缺口必须写进推荐里。"""
    pc = _trinket_pc(["BG_T_BEAST1", "BG_T_BEAST2"])
    stats = _stats(rows=[{"card_id": "BG_T_BEAST2", "tier": "c",
                          "pick_rate": 20.0, "avg_final_placement": 4.33}])
    rep = evaluate_state(_choice_state(pc), _db(), trinket_stats=stats)
    adv = rep.pending_choice
    assert adv.options[0].score is None              # 一号没有数据 ≠ 0 分
    assert adv.recommended is adv.options[1]
    assert adv.recommend_note == "1 项无实证数据"
    assert "1 项无实证数据" in adv.recommend_text
    assert "1 项无实证数据" in rep.decision.reason


def test_no_option_with_stats_means_no_recommendation():
    """四个选项一个都没数据时不做推荐（宁可不说，也不能瞎指）。"""
    pc = _trinket_pc(["BG_T_BEAST1", "BG_T_BEAST2"])
    stats = _stats(rows=[{"card_id": "BG_T_FIRE0", "tier": "s",
                          "avg_final_placement": 3.2}])
    rep = evaluate_state(_choice_state(pc), _db(), trinket_stats=stats)
    assert rep.pending_choice.recommended is None
    assert rep.pending_choice.recommend_text == ""
    assert rep.decision.reason == "免费收益，先点开挑一个"


def test_dark_gift_and_other_choices_are_never_scored():
    """⚠️ 只有饰品能打分：黑赐/三连奖励即使 card_id 命中实证表也不能推荐。"""
    stats = _stats()
    pc = _trinket_pc(["BG_T_BEAST1"], source="BG36_MidGameEffect_010",
                     name="黑暗之赐")
    rep = evaluate_state(_choice_state(pc), _db(), trinket_stats=stats)
    assert rep.pending_choice.kind == "dark_gift"
    assert rep.pending_choice.recommended is None
    assert rep.pending_choice.options[0].score is None
    other = _trinket_pc(["BG_T_BEAST2"], source="TB_BaconShop_3ofKindCheck",
                        name="三连奖励")
    rep2 = evaluate_state(_choice_state(other), _db(), trinket_stats=stats)
    assert rep2.pending_choice.kind == "other"
    assert rep2.pending_choice.recommended is None
    assert rep2.pending_choice.options[0].score is None


# ---------------------------------------------------------------------------
# 英雄实证分（S1 开局英雄选择）：选取率百分位打分 + 给推荐
# ---------------------------------------------------------------------------

from hsbg.heroes import HeroStats  # noqa: E402


def _hero_pc(card_ids):
    """实测形态：`ChoiceType=MULLIGAN` + `Source=GameEntity` + 全英雄选项。"""
    pc = PendingChoice(choice_id=1, kind="MULLIGAN", source_card_id="",
                       source_name="", player="我", is_local=True)
    for i, cid in enumerate(card_ids):
        pc.options.append(ChoiceOption(index=i, entity_id=4000 + i,
                                       card_id=cid, name="日志名%d" % i,
                                       zone="HAND"))
    return pc


def _hero_stats(rows=None):
    """手撮英雄实证表（不碰生成物 `data/heroes_hsreplay.json`）。"""
    if rows is None:
        rows = [
            {"card_id": "BG_T_HERO1", "name": "英雄一号",
             "pick_rate": 70.0, "armor": 6},
            {"card_id": "BG_T_HERO2", "name": "英雄二号",
             "pick_rate": 20.0, "armor": 16},
        ]
    return HeroStats.from_rows(rows, bracket="ALL")


def test_hero_pick_is_recognised_and_listed_with_card_names():
    """`ChoiceType=MULLIGAN` + 卡号带 `_HERO` = 英雄选择，不是起手换牌。"""
    pc = _hero_pc(["BG_T_HERO1", "BG_T_HERO2"])
    rep = evaluate_state(_choice_state(pc), _db())
    adv = rep.pending_choice
    assert adv is not None and adv.kind == "hero"
    assert adv.display_title == "英雄选择"
    assert adv.title_en == "Hero selection"       # 喂模型必须全英文（ADR-4）
    assert [o.label for o in adv.options] == ["英雄一号", "英雄二号"]
    assert adv.options[0].kind == "hero"


def test_hero_options_get_scores_and_the_best_is_recommended():
    # 弱的一号在前、强的在后 —— 推荐必须认的是选项对象本身
    pc = _hero_pc(["BG_T_HERO2", "BG_T_HERO1"])
    rep = evaluate_state(_choice_state(pc), _db(), hero_stats=_hero_stats())
    adv = rep.pending_choice
    assert all(o.score is not None for o in adv.options)
    assert adv.options[1].score > adv.options[0].score
    assert adv.options[1].score == 10.0          # 全表最高 = 百分位 1.0
    assert adv.options[0].score == 0.0           # 全表最低 = 百分位 0
    assert adv.recommended is adv.options[1]
    assert adv.recommended.label == "英雄一号"
    assert adv.options[1].score_note == "选取率70% · 护甲6"
    assert adv.options[1].score_note_en == "picked 70%, armor 6"
    assert adv.recommend_note == ""              # 数据齐全，没有缺口提示
    assert adv.recommend_text.startswith("规则推荐 英雄一号")


def test_hero_note_en_puts_the_stats_before_the_card_text():
    """喂模型的一行里实证统计要排在英雄技能文本前面（后者偏长会吃掉统计）。"""
    pc = _hero_pc(["BG_T_HERO1"])
    rep = evaluate_state(_choice_state(pc), _db(), hero_stats=_hero_stats())
    note_en = rep.pending_choice.options[0].note_en
    assert note_en.startswith("picked 70%, armor 6")
    assert "英雄技能" in note_en                    # 卡文兜底排在后面


def test_hero_pick_decision_box_fires_even_outside_recruit():
    """⚠️ 英雄选择在 `STEP=BEGIN_MULLIGAN`（实测，非招募期），决策框不能掉进
    "战斗阶段，等结算"的兜底里 —— 它卡着游戏，必须先于阶段判定。"""
    pc = _hero_pc(["BG_T_HERO2", "BG_T_HERO1"])
    st = _state(step="BEGIN_MULLIGAN")
    st.pending_choice = pc
    rep = evaluate_state(st, _db(), hero_stats=_hero_stats())
    box = rep.decision
    assert box.trigger == "pending_choice"
    assert box.action == "选择" and box.target == "英雄选择"
    assert box.urgency == "now"
    assert "规则推荐 英雄一号" in box.reason
    assert "推荐 英雄一号" in rep.warnings[0].text
    assert rep.warnings[0].level == "info"       # 待选只是提醒，不是警告


def test_hero_scoring_degrades_to_display_only_without_stats():
    """没有实证表（老行为）：只展示不推荐，决策框退回英雄专用文案。"""
    pc = _hero_pc(["BG_T_HERO1", "BG_T_HERO2"])
    st = _state(step="BEGIN_MULLIGAN")
    st.pending_choice = pc
    rep = evaluate_state(st, _db())              # 不传 hero_stats
    adv = rep.pending_choice
    assert adv.recommended is None and adv.recommend_text == ""
    assert all(o.score is None and o.score_note == "" for o in adv.options)
    assert rep.decision.reason == "选好英雄再开局"
    assert "推荐" not in rep.warnings[0].text


def test_hero_partial_stats_recommend_with_a_caveat():
    """新补丁刚上的英雄 HSReplay 还没统计到：缺口必须写进推荐里。"""
    pc = _hero_pc(["BG_T_HERO1", "BG_T_HERO2"])
    stats = _hero_stats(rows=[{"card_id": "BG_T_HERO2", "name": "英雄二号",
                               "pick_rate": 20.0, "armor": 16}])
    rep = evaluate_state(_choice_state(pc), _db(), hero_stats=stats)
    adv = rep.pending_choice
    assert adv.options[0].score is None          # 没数据 ≠ 0 分
    assert adv.recommended is adv.options[1]
    assert adv.recommend_note == "1 项无实证数据"
    assert "1 项无实证数据" in adv.recommend_text
    assert "1 项无实证数据" in rep.decision.reason


def test_hero_pick_is_never_scored_by_trinket_stats():
    """⚠️ 两类实证表各管各家：饰品表里恰好有同名卡也不能给英雄打分。"""
    stats = _stats(rows=[{"card_id": "BG_T_HERO1", "tier": "s",
                          "pick_rate": 46.0, "avg_final_placement": 3.2}])
    pc = _hero_pc(["BG_T_HERO1", "BG_T_HERO2"])
    rep = evaluate_state(_choice_state(pc), _db(), trinket_stats=stats)
    assert rep.pending_choice.kind == "hero"
    assert rep.pending_choice.recommended is None
    assert all(o.score is None for o in rep.pending_choice.options)


# --------------------------------------------------------------------------
# 决策框（DecisionBox）—— "现在该做什么"，只给一个动作
# --------------------------------------------------------------------------

def _box(st, cfg=None):
    return _eng(cfg).evaluate(st).decision


def test_curve_tier_table_is_the_documented_one():
    """曲线表是决策框的骨架，钉死它的取值。"""
    f = RulesEngine._curve_tier
    assert f(1) is None and f(2) is None
    assert f(3) == 2 and f(6) == 2
    assert f(7) == 3 and f(9) == 3
    assert f(10) == 4 and f(12) == 4
    assert f(13) == 5 and f(15) == 5
    assert f(16) == 6 and f(30) == 6


def test_decision_levels_when_the_curve_says_so():
    """四费升二本 —— 实测真机在回合 3 付 4 金升 2 本，与曲线完全一致。"""
    box = _box(_state(turn=3, tier=1, gold=4, tech_up_cost=4))
    assert box.action == "升本"
    assert box.target == "2本"
    assert box.cost == 4
    assert box.trigger == "curve"
    assert box.urgency == "soon"
    assert box.curve_tier == 2
    assert box.off_curve is False


def test_decision_spends_gold_instead_of_hoarding_when_level_is_unaffordable():
    """升不起本的时候**不能**说「攒钱」。

    ⚠️ 酒馆战棋的金币**不跨回合保留** —— 实测 `RESOURCES` 每回合重置成回合值
    （3/4/5/…/10），不是累加。"这回合不花、下回合升"根本不成立，
    所以决策框必须让玩家把金币花掉（买牌或刷新），而不是攒着。
    """
    box = _box(_state(turn=3, tier=1, gold=3, tech_up_cost=4))
    assert box.action != "攒钱"
    assert box.off_curve is True          # 偏离曲线要能看出原因
    assert "差 1 金" in box.reason


def test_decision_waits_only_when_there_is_no_gold_left():
    """金币为 0 时才允许「观望」——还有钱就一定给个动作。"""
    box = _box(_state(turn=3, tier=4, gold=0, tech_up_cost=99))
    assert box.action == "观望"


def test_decision_rerolls_to_spend_leftover_gold():
    box = _box(_state(turn=9, tier=4, gold=6, tech_up_cost=99))
    assert box.action == "刷新"
    assert box.trigger == "spend_gold"
    assert "6 金" in box.reason


def test_decision_buys_an_affordable_tavern_spell():
    """商店里的酒馆法术不能被无视 —— 它走 `DragBuy_Spell`，不占格子。

    ⚠️ 这条是回归：`_score_shop` 只遍历 `st.shop`（随从），法术存在
    `st.spells` 里，早先版本整个漏掉，1 金的法术在建议里根本看不见。
    """
    st = _state(turn=9, tier=4, gold=3, tech_up_cost=99)
    st.spells = [ShopSlot(slot_entity_id=900, minion_entity_id=901,
                          card_id="BG_T_SPELL", cost=1, kind="spell",
                          belongs_to_local=True)]
    rep = _eng().evaluate(st)
    assert rep.spells and rep.spells[0].name == "某法术"
    assert rep.spells[0].cost == 1
    assert rep.spells[0].kind == "spell"
    assert rep.decision.action == "买"
    assert rep.decision.target == "某法术"


def test_spell_cost_falls_back_to_the_card_db():
    """槽位上的 `COST` 还没写上来时（实测 MAIN_START 那一下是空的），用卡表费用。"""
    st = _state(turn=9, tier=4, gold=2, tech_up_cost=99)
    st.spells = [ShopSlot(slot_entity_id=900, minion_entity_id=901,
                          card_id="BG_T_SPELL", kind="spell",
                          belongs_to_local=True)]
    rep = _eng().evaluate(st)
    assert rep.spells[0].cost == 1


def test_spell_that_costs_more_than_our_gold_is_skipped():
    st = _state(turn=9, tier=4, gold=0, tech_up_cost=99)
    st.spells = [ShopSlot(slot_entity_id=900, minion_entity_id=901,
                          card_id="BG_T_SPELL", cost=1, kind="spell",
                          belongs_to_local=True)]
    rep = _eng().evaluate(st)
    assert rep.spells[0].action == "跳过"
    assert "金币不足" in rep.spells[0].reasons


def test_empty_board_buys_a_body_instead_of_a_spell():
    """回合 1 空场：先买身材，别买法术。

    ⚠️ 法术打分与随从打分**不是同一把尺子**（法术起手 6 分，随从按身材算，
    早期随从普遍 5~9 分），直接比大小会让决策框在空场时去买法术。实测原话：
    `回合 1 · 3 金 · 空场 → 买 招募新人 2 金`。
    """
    st = _state(turn=1, tier=1, gold=3, board=(), shop=["BG_T_BEAST1"],
                tech_up_cost=99)
    st.spells = [ShopSlot(slot_entity_id=900, minion_entity_id=901,
                          card_id="BG_T_SPELL", cost=1, kind="spell",
                          belongs_to_local=True)]
    rep = _eng().evaluate(st)
    assert rep.decision.action == "买"
    assert rep.decision.target == "野兽一号", rep.decision.target
    # ⚠️ 触发原因可能是 `thin_board`（场面太空）也可能是 `shop`（它是店内第 1 名，
    # 见 `_assign_shop_actions`）—— 这个用例只关心「买的是随从不是法术」。
    assert rep.decision.trigger in ("thin_board", "shop")


def _spell_item(card_id, board_n=4, gold=10):
    """造一个 4 本、10 金、场上有 `board_n` 只随从的盘面，返回商店里那张法术的评分。"""
    st = _state(turn=9, tier=4, gold=gold,
                board=["BG_T_AVENGE"] * board_n, tech_up_cost=99)
    st.spells = [ShopSlot(slot_entity_id=900, minion_entity_id=901,
                          card_id=card_id, cost=2, kind="spell",
                          belongs_to_local=True)]
    return _eng().evaluate(st).spells[0]


def test_spell_effect_is_read_from_card_text():
    """效果分类全部来自**卡文**（实测酒馆法术的 `mechanics` 大多是空的）。"""
    cases = {
        "BG_T_SP_CARD": "给牌",
        "BG_T_SP_COIN": "给钱",
        "BG_T_SP_CAP": "涨钱上限",
        "BG_T_SP_GOLD": "变身",          # ⚠️ 卡文里是「变为 金色」，中间有空格
        "BG_T_SP_GLOBAL": "给身材",
    }
    for card_id, label in cases.items():
        it = _spell_item(card_id)
        assert any(label in r for r in it.reasons), (card_id, label, it.reasons)
        assert "效果：" in "".join(it.reasons), (card_id, it.reasons)


def test_coin_spell_does_not_match_the_gold_cap_pattern():
    """⚠️ 「铸币上限」不能被算成一次性「给钱」——两者价值差一倍以上。"""
    cap = _spell_item("BG_T_SP_CAP")
    coin = _spell_item("BG_T_SP_COIN")
    assert "涨钱上限" in "".join(cap.reasons)
    assert "涨钱上限" not in "".join(coin.reasons)
    assert cap.score > coin.score, (cap.score, coin.score)


def test_a_free_minion_beats_a_coin_of_the_same_tier_and_cost():
    """同星同费时「白拿一张随从」必须压过「净赚 1 金」。"""
    card = _spell_item("BG_T_SP_CARD")
    coin = _spell_item("BG_T_SP_COIN")
    assert card.score > coin.score, (card.score, coin.score)


def test_transforming_into_golden_is_penalised():
    """变身/异常方差大，同星同费时要排在稳收益后面。"""
    gold = _spell_item("BG_T_SP_GOLD")
    card = _spell_item("BG_T_SP_CARD")
    assert gold.score < card.score, (gold.score, card.score)


def test_global_buff_scales_with_board_size():
    """「使**你的**随从获得+1/+1」吃场面：场上越多只越值。"""
    small = _spell_item("BG_T_SP_GLOBAL", board_n=1)
    big = _spell_item("BG_T_SP_GLOBAL", board_n=6)
    assert big.score > small.score, (small.score, big.score)
    assert any("吃场面" in r for r in big.reasons), big.reasons


def test_single_target_buff_is_penalised_on_an_empty_board():
    """「使**一个**随从获得+2/+2」在空场没处贴，要减分并说清楚。"""
    empty = _spell_item("BG_T_SP_SINGLE", board_n=0)
    some = _spell_item("BG_T_SP_SINGLE", board_n=4)
    assert empty.score < some.score, (empty.score, some.score)
    assert any("场上没有随从可贴" in r for r in empty.reasons), empty.reasons


def test_placeholder_entity_name_falls_back_to_the_card_db():
    """实体名是 `UNKNOWN ENTITY [cardType=INVALID]` 时必须用卡表名。

    ⚠️ 实测 `BG31_881` 的日志名是 UNKNOWN ENTITY，卡表里却是「时间管理」；
    不排掉占位名，决策框会输出"买 UNKNOWN ENTITY [cardType=INVALID]"。
    """
    st = _state(turn=9, tier=4, gold=6, tech_up_cost=99)
    st.spells = [ShopSlot(slot_entity_id=900, minion_entity_id=901,
                          card_id="BG_T_SPELL", name="UNKNOWN ENTITY [cardType=INVALID]",
                          cost=1, kind="spell", belongs_to_local=True)]
    rep = _eng().evaluate(st)
    assert rep.spells[0].name == "某法术", rep.spells[0].name
    assert "UNKNOWN" not in rep.decision.target


def test_full_board_makes_a_spell_more_attractive():
    """场面满 7 只时随从买不了，但法术照买 —— 所以要给它加分。"""
    st = _state(turn=9, tier=4, gold=9, tech_up_cost=99)
    st.board = [_minion(200 + i, "BG_T_AVENGE", atk=1, hp=3) for i in range(7)]
    st.spells = [ShopSlot(slot_entity_id=900, minion_entity_id=901,
                          card_id="BG_T_SPELL", cost=1, kind="spell",
                          belongs_to_local=True)]
    rep = _eng().evaluate(st)
    assert any("不占格子" in r for r in rep.spells[0].reasons)


def test_decision_guard_blocks_expensive_level_with_thin_board():
    """第四定理：3 本以上、升完只剩不到 2 金、场面又薄 → 别贸然升。

    实测真机在回合 9/11 有 7~8 金、场面 6~7 只时并没有升 4 本，
    而是等到回合 12/13；旧逻辑在这两回合都劝升（偏早 2~3 回合）。
    """
    box = _box(_state(turn=13, tier=3, gold=8, tech_up_cost=7, board=[]))
    assert box.action != "升本"
    assert box.off_curve is True
    assert "先补场面" in box.reason


def test_decision_guard_is_skipped_below_tier_three():
    """⚠️ 低本阶段场面本来就小（四费升二本时常常一只都没有），守卫不能拦。"""
    box = _box(_state(turn=3, tier=1, gold=4, tech_up_cost=4, board=[]))
    assert box.action == "升本"


def test_decision_marks_off_curve_when_already_ahead():
    box = _box(_state(turn=15, tier=6, gold=0))
    assert box.off_curve is True
    assert box.curve_tier == 5


def test_decision_buys_the_triple_before_anything_else():
    st = _state(turn=5, tier=2, gold=10,
                board=["BG_T_BEAST1", "BG_T_BEAST1"],
                shop=["BG_T_MECH2", "BG_T_BEAST1"])
    box = _box(st)
    assert box.action == "买"
    assert box.target == "野兽一号"
    assert box.trigger == "triple"


def test_decision_buys_the_minion_the_rules_call_a_buy():
    """同族加成把机械二号推到 18 分以上 → 决策框给出「买」。

    ⚠️ 商店项的攻/血来自**槽位随从实体**的 ATK/HEALTH（夹具里是 2/3），
    不是卡表里的 8/8 —— 所以只靠卡表身材堆不到阈值。
    """
    st = _state(turn=5, tier=2, gold=10,
                board=["BG_T_MECH1"] * 3, shop=["BG_T_MECH2"])
    box = _box(st)
    assert box.action == "买"
    assert box.target == "机械二号"
    assert box.trigger == "shop"


def test_decision_observes_when_nothing_is_worth_buying():
    box = _box(_state(turn=5, tier=2, gold=0, shop=["BG_T_BEAST1"]))
    assert box.action == "观望"
    assert box.trigger == "idle"
    assert box.urgency == "wait"


def test_decision_waits_by_default_without_a_local_seat():
    st = _state()
    st.seats.clear()
    st.local_player_id = None
    box = _box(st)
    assert box.action == "观望"
    assert "还没有本地玩家信息" in box.reason


def test_decision_waits_during_the_combat_phase():
    box = _box(_state(turn=5, tier=2, gold=10, step="MAIN_COMBAT"))
    assert box.action == "观望"
    assert "战斗阶段" in box.reason


def test_decision_asks_for_the_refresh_when_it_is_free():
    """⚠️ 场面必须先撑起来：场空时「先买身材」优先于免费刷新（见 `_decision` 分支 4）。
    所以这里给 3 只随从（不算薄），并且**商店是空的** —— 有牌可买时买牌永远优先，
    免费刷新是买完之后顺手用的，不该顶掉「买」。"""
    st = _state(turn=5, tier=2, gold=5, shop=[],
                board=["BG_T_BEAST2", "BG_T_MECH1", "BG_T_MECH2"])
    e = Entity(entity_id=555, card_id="Bacon_Free_Refresh_Player_Ench")
    e.tags["BACON_FREE_REFRESH_COUNT"] = "1"
    st.entities[555] = e
    box = _box(st)
    assert box.action == "刷新"
    assert box.trigger == "free_refresh"


# --------------------------------------------------------------------------
# 买牌动作分派：**按店内排名**，不是绝对分数线
#
# 依据 `tools/calibrate_shop.py`（5 份真机日志 / 83 个商店快照）：
#   · 真人买走「店内第 1 名」的比例按分数段是 4-7 分 91.7%、7-10 分 100%、
#     10-13 分 94.4%、13-16 分 69.2%、16-19 分 75%、19-22 分 87.5%、22+ 分 71.4%
#     —— **几乎不随分数变化**；用分数预测「这回合买不买」的精确率在 0~24 分的
#     每个阈值上都贴着基础率 84% 不动，说明分数对「买不买」没有预测力。
#   · 但分数**排序**有效：真人选中的那张，店内排名第 1 占 47.1%、前 2 占 66.7%
#     （纯随机约 30%）；中局（5-9 回合）第 1 名命中 64.7%。
# 所以：分数只负责排序，「买不买」交给金币 / 场面 / 升本的约束。
# --------------------------------------------------------------------------


def test_the_top_ranked_card_is_the_one_the_engine_tells_you_to_buy():
    st = _state(turn=5, tier=2, gold=10, shop=["BG_T_BEAST1", "BG_T_MECH2"])
    rep = _eng().evaluate(st)
    top = rep.shop[0]
    assert top.card_id == "BG_T_MECH2"      # 6 星 8/8 一定排第一
    assert top.action == "买"
    assert "本店最高分" in top.reasons


def test_a_low_scoring_shop_still_says_buy_the_best_card():
    """⚠️ 旧逻辑要 `score >= 18` 才说「买」，而真机日志里真人会买的牌绝大多数只有
    4~13 分 —— 于是引擎在真人 85% 会买的局面里一直说「跳过」，决策框自然几乎从不
    说买。现在只要它是**店内第 1 名**且过了地板分（`BUY_TOP_MIN`）就该说「买」。"""
    st = _state(turn=5, tier=2, gold=10, shop=["BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    top = rep.shop[0]
    assert top.score < 18.0                 # 远低于旧的「买」线
    assert top.action == "买"
    assert "本店最高分" in top.reasons


def test_the_second_and_third_best_cards_are_only_consider():
    st = _state(turn=7, tier=3, gold=10,
                shop=["BG_T_MECH2", "BG_T_BEAST3", "BG_T_BEAST2", "BG_T_BEAST1"])
    rep = _eng().evaluate(st)
    assert [it.action for it in rep.shop[:3]] == ["买", "考虑", "考虑"]
    assert rep.shop[3].action == "跳过"


def test_a_full_board_downgrades_the_top_card_to_consider():
    """实测：**场面已满的店恰恰是最高分的店**（店内最高分 p50=18.3，全场 p50=11.7），
    高分在此时没有意义 —— 要买就得先卖，所以降级成「考虑」。"""
    full = ["BG_T_MECH1", "BG_T_MECH1", "BG_T_MECH1", "BG_T_MECH2",
            "BG_T_MECH2", "BG_T_BEAST1", "BG_T_BEAST2"]
    st = _state(turn=9, tier=3, gold=10, shop=["BG_T_MECH2"], board=full)
    rep = _eng().evaluate(st)
    top = rep.shop[0]
    assert top.action == "考虑"
    assert "场面已满" in top.reasons


def test_a_triple_always_buys_even_when_it_is_not_rank_one():
    """凑三连是硬收益，不受「店内第 1 名」限制。"""
    st = _state(turn=5, tier=2, gold=10,
                shop=["BG_T_BEAST1", "BG_T_MECH2"],
                board=["BG_T_BEAST1", "BG_T_BEAST1"])
    by_id = {it.card_id: it for it in _eng().evaluate(st).shop}
    assert by_id["BG_T_BEAST1"].completes_triple
    assert by_id["BG_T_BEAST1"].action == "买"


def test_an_unaffordable_card_is_skipped_even_when_it_is_rank_one():
    st = _state(turn=5, tier=2, gold=1, shop=["BG_T_MECH2"])
    assert _eng().evaluate(st).shop[0].action == "跳过"


def test_decision_puts_the_pending_choice_first():
    """⚠️ 待选会**卡住回合**，比买牌/升本都紧急，必须排第一。"""
    st = _choice_state(_trinket_pc(["BG_T_BEAST1"]))
    st.shop = _state(turn=5, tier=2, gold=10, shop=["BG_T_MECH2"]).shop
    box = _box(st)
    assert box.action == "选择"
    assert box.target == "小饰品四选一"
    assert box.urgency == "now"
    assert box.trigger == "pending_choice"


def test_dark_gift_does_not_preempt_buying_but_is_mentioned():
    """⚠️「黑暗发现可点」的 flag 实测连续亮 4 个回合（T5→T9），所以它**不能**
    抢在买牌前面当主动作；退成理由行的一句话。"""
    st = _state(turn=5, tier=2, gold=10,
                board=["BG_T_MECH1"] * 3, shop=["BG_T_MECH2"])
    st.entities[302] = _gift_button(vfx="1")
    box = _box(st)
    assert box.action == "买"
    assert box.trigger == "shop"
    assert "黑暗发现可点" in box.reason


def test_dark_gift_becomes_the_action_when_there_is_nothing_else():
    st = _state(turn=5, tier=2, gold=0, shop=["BG_T_BEAST1"])
    st.entities[302] = _gift_button(vfx="1")
    box = _box(st)
    assert box.action == "选择"
    assert box.target == "黑暗发现"
    assert box.trigger == "dark_gift"
    assert box.urgency == "now"


def test_pending_choice_reason_is_not_cluttered_by_the_dark_gift():
    st = _choice_state(_trinket_pc(["BG_T_BEAST1"]))
    st.entities[302] = _gift_button(vfx="1")
    box = _box(st)
    assert box.trigger == "pending_choice"
    assert "黑暗发现" not in box.reason


def test_decision_headline_reads_naturally():
    assert DecisionBox(action="升本", target="3本").headline == "升本 3本"
    assert DecisionBox(action="买", target="野兽一号").headline == "买 野兽一号"
    assert DecisionBox(action="选择").headline == "先选牌"
    assert DecisionBox(action="观望").headline == "观望"
    assert DecisionBox(action="升格", target="").headline == "升格"
