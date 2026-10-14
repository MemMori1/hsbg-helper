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
    AdviceReport, LevelAdvice, RulesConfig, RulesEngine,
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
    # 黑暗之赐的**赐福**（另一张卡）：实测 `BG36_MidGameEffect_000t13`
    # 就是「圣盾 + 风怒」这种纯词条赐福
    Card(card_id="BG_T_GIFT1", name="测试赐福", type="SPELL", tech_level=None,
         attack=None, health=None, cost=None, race=None, races=(),
         mechanics=("DIVINE_SHIELD", "WINDFURY"),
         text="<b>圣盾</b>。<b>风怒</b>", pool_minion=False),
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
    st = _state(tier=2, gold=2, tech_up_cost=7)
    adv = _eng().evaluate(st).level
    assert adv.action == "不升", (adv.action, adv.score)
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
# 待选（饰品四选一 / 黑赐三选一）：规则层只做展示 + 一行特征，**不打分**
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
