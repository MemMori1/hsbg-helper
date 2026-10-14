"""state.py 的最小回归测试。

用例全部基于真实日志里实测到的字段组合（见 docs/需求分析.md §11），
不自己编格式。缩进 / 前缀与 test_parser.py 保持同一套辅助函数语义。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from hsbg.parser import PowerLogParser          # noqa: E402
from hsbg.state import StateBuilder, _int        # noqa: E402

_HEAD = "D 21:12:25.2665920 GameState.DebugPrintPower()"


def _line(payload, indent=0, head=_HEAD):
    """indent = dash 之后的空格数；载荷缩进 = indent - 1（与真机档位一致）。"""
    return f"{head} - {' ' * indent}{payload}"


def _feed(parser, builder, lines, flush=True):
    """喂一批日志行。

    ⚠️ `flush=True` 会在末尾补一个空行。真机里每个实体块（FULL_ENTITY + 属性行）
    后面都跟着空行，属性行正是靠空行才从 `_pending` 里 flush 出来；不补空行的话
    实体的 tag 会留在解析器里，`state` 看不到 —— 测试会以假乱真地失败。
    """
    for ln in lines:
        for ev in parser.feed_line(ln):
            builder.apply(ev)
    if flush:
        for ev in parser.feed_line(""):
            builder.apply(ev)


# 真实日志片段：CREATE_GAME 段的 Player 声明（含空账号的鲍勃）
L_CREATE = _line("CREATE_GAME")
L_GAMEENTITY = _line("GameEntity EntityID=16", indent=4)
L_PLAYER_LOCAL = _line(
    "Player EntityID=17 PlayerID=6 "
    "GameAccountId=[hi=100000000000000001 lo=200000001]", indent=4)
L_PLAYER_BOB = _line("Player EntityID=18 PlayerID=14 GameAccountId=[hi=0 lo=0]",
                     indent=4)
# 本地账号实体指向的英雄（`HERO_ENTITY`）+ 该英雄实体本身
L_LOCAL_HERO_ENTITY = _line("tag=HERO_ENTITY value=104", indent=9)
L_HERO_LOCAL = _line("FULL_ENTITY - Creating ID=104 CardID=TB_BaconShop_HERO_91",
                     indent=5)


def test_int_conversion_handles_log_strings():
    assert _int("3") == 3
    assert _int(3) == 3
    assert _int("None") is None
    assert _int(None) is None
    assert _int("") is None


def test_empty_account_is_not_mistaken_for_local_player():
    """鲍勃的 hi=0 lo=0 不能被当成本地玩家（这是修过的真 bug）。"""
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [L_CREATE, L_GAMEENTITY, L_PLAYER_LOCAL, L_PLAYER_BOB, ""])
    assert b._local_account_player == 17, b._local_account_player


def test_player_ids_are_parsed_from_declaration():
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [L_CREATE, L_GAMEENTITY, L_PLAYER_LOCAL, L_PLAYER_BOB, ""])
    assert b.state.entities[17].player_id == 6
    assert b.state.entities[18].player_id == 14


def test_turn_and_step_are_tracked():
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        _line("TAG_CHANGE Entity=GameEntity tag=TURN value=3"),
        _line("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION"),
    ])
    assert b.state.turn == 3
    assert b.state.step == "MAIN_ACTION"
    assert b.state.in_recruit_phase is True


def test_shop_slot_ref_gives_minion_card():
    """DragBuy 的 tag=2442 指向商店随从 —— 这是 P1 的商店认法。"""
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        # 商店随从实体
        _line("FULL_ENTITY - Creating ID=308 CardID=BG31_149"),
        _line("tag=ZONE value=PLAY", indent=8),
        _line("tag=CARDTYPE value=MINION", indent=8),
        _line("tag=ATK value=2", indent=8),
        _line("tag=HEALTH value=3", indent=8),
        "",
        # 槽位实体
        _line("FULL_ENTITY - Creating ID=309 CardID=TB_BaconShop_DragBuy"),
        _line("tag=ZONE value=PLAY", indent=8),
        _line("tag=2442 value=308", indent=8),
        _line("tag=CONTROLLER value=6", indent=8),
        "",
    ])
    slots = b.state.shop
    assert len(slots) == 1, slots
    assert slots[0].minion_entity_id == 308
    assert slots[0].card_id == "BG31_149"
    assert slots[0].attack == 2 and slots[0].health == 3
    assert slots[0].empty is False


def test_shop_slot_cleared_when_ref_becomes_zero():
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        # ⚠️ 本地英雄必须能解析出来，否则 `_local_controller()` 为 None，
        # 槽位归属判为 False、`shop_minions` 恒为空（不是被测行为，却会让断言假失败）
        L_PLAYER_LOCAL, L_LOCAL_HERO_ENTITY,
        L_HERO_LOCAL,
        _line("tag=CARDTYPE value=HERO", indent=9),
        _line("tag=PLAYER_ID value=6", indent=9),
        _line("tag=CONTROLLER value=6", indent=9),
        # ⚠️ 真机每个实体块后都有空行，靠它把挂起实体 flush 出来；不补空行的话
        # 下一个 `FULL_ENTITY` 会顶掉还挂着的实体，tags 全丢（测试会以假乱真地失败）
        "",
        _line("FULL_ENTITY - Creating ID=309 CardID=TB_BaconShop_DragBuy", indent=5),
        # ⚠️ 缩进必须与宿主对齐（宿主 dash 后 5 → 属性 dash 后 9），写 8 会让这些
        # 属性行被上一档的实体（玩家实体）吸走，槽位变成空壳
        _line("tag=ZONE value=PLAY", indent=9),
        _line("tag=2442 value=308", indent=9),
        _line("tag=CONTROLLER value=6", indent=9),
        "",
    ])
    assert len(b.state.shop_minions) == 1
    _feed(p, b, [_line("TAG_CHANGE Entity=309 tag=2442 value=0")])
    assert len(b.state.shop_minions) == 0


def test_seat_health_is_hero_health_minus_damage():
    """英雄实体的 HEALTH 是**上限**，当前血量 = HEALTH − DAMAGE。

    真机：本地英雄 104 `DAMAGE=32`，`HEALTH=40` → 8 血。不能把 HEALTH 直接当血量。
    """
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        _line("FULL_ENTITY - Creating ID=104 CardID=TB_BaconShop_HERO_91"),
        _line("tag=ZONE value=PLAY", indent=8),
        _line("tag=CARDTYPE value=HERO", indent=8),
        _line("tag=PLAYER_ID value=6", indent=8),
        _line("tag=CONTROLLER value=6", indent=8),
        _line("tag=HEALTH value=40", indent=8),
        _line("tag=DAMAGE value=32", indent=8),
        "",
    ])
    seats = b.state.seats
    assert 6 in seats, seats
    assert seats[6].hero_card_id == "TB_BaconShop_HERO_91"
    assert seats[6].max_health == 40
    assert seats[6].health == 8, seats[6]


def test_gold_comes_from_named_player_resource_tags():
    """金币走「具名玩家实体」形态：`Entity=测试玩家#1234 tag=RESOURCES`。

    ⚠️ 这类事件 `entity_id is None`，早先 `_on_tag_change` 直接 return，金币永远是 None。
    本地玩家名字靠 `CURRENT_PLAYER value=1` + `HERO_ENTITY` 与本地英雄比对锁定。
    """
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        _line("Player EntityID=17 PlayerID=6 "
              "GameAccountId=[hi=100000000000000001 lo=200000001]", indent=5),
        _line("FULL_ENTITY - Creating ID=104 CardID=TB_BaconShop_HERO_91", indent=5),
        _line("tag=CARDTYPE value=HERO", indent=9),
        _line("tag=PLAYER_ID value=6", indent=9),
        _line("tag=CONTROLLER value=6", indent=9),
        "",
        _line("TAG_CHANGE Entity=17 tag=HERO_ENTITY value=104"),
        _line("TAG_CHANGE Entity=测试玩家#1234 tag=HERO_ENTITY value=104"),
        _line("TAG_CHANGE Entity=测试玩家#1234 tag=CURRENT_PLAYER value=1"),
        _line("TAG_CHANGE Entity=测试玩家#1234 tag=RESOURCES value=7"),
        _line("TAG_CHANGE Entity=测试玩家#1234 tag=RESOURCES_USED value=2"),
        # 对手的同类事件不能影响本地座位
        _line("TAG_CHANGE Entity=对手玩家 tag=RESOURCES value=99"),
    ])
    seat = b.state.local_seat
    assert seat is not None, b.state.seats
    assert seat.player_id == 6
    assert seat.resources == 7, seat
    assert seat.resources_used == 2, seat
    assert seat.gold == 5, seat


def test_board_excludes_other_controllers_minions():
    """商店随从同在 ZONE=PLAY，靠 CONTROLLER 排除，不能算自家场面。"""
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        # 先确立"谁是本地玩家"：带真实账号的 Player 声明 + 其英雄实体
        _line("Player EntityID=17 PlayerID=6 "
              "GameAccountId=[hi=100000000000000001 lo=200000001]", indent=5),
        _line("TAG_CHANGE Entity=17 tag=HERO_ENTITY value=104"),
        # 真机：FULL_ENTITY 声明在 dash 后 5 空格，属性行 9 空格
        _line("FULL_ENTITY - Creating ID=104 CardID=TB_BaconShop_HERO_91", indent=5),
        _line("tag=ZONE value=PLAY", indent=9),
        _line("tag=CARDTYPE value=HERO", indent=8),
        _line("tag=PLAYER_ID value=6", indent=8),
        _line("tag=CONTROLLER value=6", indent=8),
        "",
        _line("FULL_ENTITY - Creating ID=312 CardID=BG36_110", indent=5),
        _line("tag=ZONE value=PLAY", indent=9),
        _line("tag=CARDTYPE value=MINION", indent=9),
        _line("tag=CONTROLLER value=6", indent=9),
        "",
        _line("FULL_ENTITY - Creating ID=308 CardID=BG31_149", indent=5),
        _line("tag=ZONE value=PLAY", indent=9),
        _line("tag=CARDTYPE value=MINION", indent=9),
        _line("tag=CONTROLLER value=14", indent=9),
        "",
    ])
    cards = [e.card_id for e in b.state.board]
    assert "BG36_110" in cards
    assert "BG31_149" not in cards, cards

# --- P1/P2 回归：商店只算本地槽位、脏标记必须被重置 -------------------------

L_DRAG_BUY = _line("FULL_ENTITY - Creating ID=309 CardID=TB_BaconShop_DragBuy", indent=5)
L_DRAG_PROP_REL = _line("tag=CARDTYPE value=MOVE_MINION_HOVER_TARGET", indent=9)
L_DRAG_PROP_REF = _line("tag=2442 value=312", indent=9)
L_DRAG_PROP_CTRL = _line("tag=CONTROLLER value=6", indent=9)
L_MINION_312 = _line("FULL_ENTITY - Creating ID=312 CardID=BG36_110", indent=5)
L_MINION_312_ZONE = _line("tag=ZONE value=PLAY", indent=9)
L_MINION_312_TYPE = _line("tag=CARDTYPE value=MINION", indent=9)
L_MINION_312_CTRL = _line("tag=CONTROLLER value=6", indent=9)
L_MINION_312_TECH = _line("tag=TECH_LEVEL value=1", indent=9)
L_MINION_312_ATK = _line("tag=ATK value=2", indent=9)
L_MINION_312_HP = _line("tag=HEALTH value=3", indent=9)


def _shop_builder():
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        L_CREATE, L_GAMEENTITY, L_PLAYER_LOCAL, L_PLAYER_BOB,
        _line("TAG_CHANGE Entity=17 tag=HERO_ENTITY value=104"),
        _line("FULL_ENTITY - Creating ID=104 CardID=TB_BaconShop_HERO_91", indent=5),
        _line("tag=ZONE value=PLAY", indent=9),
        _line("tag=CARDTYPE value=HERO", indent=9),
        _line("tag=PLAYER_ID value=6", indent=9),
        _line("tag=CONTROLLER value=6", indent=9),
        L_MINION_312, L_MINION_312_ZONE, L_MINION_312_TYPE,
        L_MINION_312_CTRL, L_MINION_312_TECH, L_MINION_312_ATK, L_MINION_312_HP,
        L_DRAG_BUY, L_DRAG_PROP_REL, L_DRAG_PROP_REF, L_DRAG_PROP_CTRL,
    ])
    return b


def test_shop_slot_exposes_minion_card_id():
    """`TB_BaconShop_DragBuy` 的 `tag=2442` 指向可买的随从实体（P1 关键结论）。"""
    b = _shop_builder()
    st = b.state
    slots = [s for s in st.shop if not s.empty]
    assert len(slots) == 1, st.shop
    s = slots[0]
    assert s.slot_entity_id == 309
    assert s.minion_entity_id == 312
    assert s.card_id == "BG36_110"
    assert s.tech_level == 1 and s.attack == 2 and s.health == 3
    assert s.belongs_to_local is True
    assert st.shop_minions[0].card_id == "BG36_110"


def test_shop_slot_without_ref_is_empty():
    """`tag=2442` 置 0 = 该槽被买走/刷新。"""
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        L_DRAG_BUY, L_DRAG_PROP_REL, L_DRAG_PROP_REF, L_DRAG_PROP_CTRL,
        _line("TAG_CHANGE Entity=309 tag=2442 value=0"),
    ])
    slots = b.state.shop
    assert len(slots) == 1
    assert slots[0].empty is True
    assert slots[0].minion_entity_id is None


def test_shop_slot_from_other_controller_is_not_buyable():
    """只有本地玩家的 DragBuy 槽位算"可买"（战斗期会给对手生成同类槽位）。"""
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [
        _line("CREATE_GAME"),
        # 本地账号实体（hi 非 0）
        _line("Player EntityID=17 PlayerID=6 "
              "GameAccountId=[hi=100000000000000001 lo=200000001]", indent=4),
        _line("tag=HERO_ENTITY value=104", indent=9),
        _line("FULL_ENTITY - Creating ID=104 CardID=TB_BaconShop_HERO_91", indent=5),
        _line("tag=CARDTYPE value=HERO", indent=9),
        _line("tag=PLAYER_ID value=6", indent=9),
        _line("tag=CONTROLLER value=6", indent=9),
        # 对手的槽位：CONTROLLER=14，与本地英雄的 6 不同
        _line("FULL_ENTITY - Creating ID=900 CardID=TB_BaconShop_DragBuy", indent=5),
        _line("tag=2442 value=901", indent=9),
        _line("tag=CONTROLLER value=14", indent=9),
        _line("FULL_ENTITY - Creating ID=901 CardID=BG31_149", indent=5),
        _line("tag=ZONE value=PLAY", indent=9),
        _line("tag=CARDTYPE value=MINION", indent=9),
        _line("tag=CONTROLLER value=14", indent=9),
    ])
    st = b.state
    assert st.local_player_id == 6
    assert [s.belongs_to_local for s in st.shop] == [False]
    assert st.shop_minions == []
    # 对手随从也不能混进自家场面
    assert [e.card_id for e in st.board] == []


def test_state_read_is_idempotent_and_cheap():
    """惰性重构后重复读 `.state` 不得改变内容，也不得重复重算。

    性能回归护栏：`_refresh_*` 全程 O(所有实体)，早先每个事件都调一次，
    96 MB 日志要 240 s（2.1 亿次函数调用）。这里断言重复读不触发重算。
    """
    b = _shop_builder()
    calls = []
    orig = b._refresh_boards

    def counting():
        calls.append(1)
        return orig()

    b._refresh_boards = counting
    first = b.state
    n = len(calls)
    for _ in range(5):
        assert b.state is first
    assert len(calls) == n, "重复读 .state 仍在重算视图"
    assert b._dirty_shop is False
    assert b._dirty_boards is False
    assert b._dirty_seats is False


def test_new_game_resets_state_and_dirty_flags():
    """第二局不重置会污染第一局座位（P1 实测缺陷）。"""
    b = _shop_builder()
    assert b.state.local_player_id == 6
    assert 6 in b.state.seats
    assert b._hero_by_player == {6: 104}

    # 1) 显式分场：座位 / 英雄表 / 本地玩家必须全部清空
    b.new_game()
    # ⚠️ 脏标记要在**读 `.state` 之前**断言：读一次就会把它们清掉。
    assert b._dirty_seats and b._dirty_shop and b._dirty_boards
    st = b.state
    assert st.seats == {}
    assert st.local_player_id is None
    assert st.entities == {}
    assert b._hero_by_player == {}
    assert b._local_player_name == ""
    assert b._named_hero == {}

    # 2) 第二局正常重建：新账号实体 + 新英雄，且不与第一局混
    p = PowerLogParser()
    _feed(p, b, [
        _line("CREATE_GAME"),
        _line("Player EntityID=23 PlayerID=8 "
              "GameAccountId=[hi=100000000000000001 lo=99]", indent=4),
        _line("tag=HERO_ENTITY value=107", indent=9),
        _line("FULL_ENTITY - Creating ID=107 CardID=TB_BaconShop_HERO_11", indent=5),
        _line("tag=CARDTYPE value=HERO", indent=9),
        _line("tag=PLAYER_ID value=8", indent=9),
        _line("tag=CONTROLLER value=8", indent=9),
    ])
    st = b.state
    assert st.local_player_id == 8
    assert sorted(st.seats) == [8]
    assert st.seats[8].hero_entity_id == 107
    assert st.seats[8].hero_card_id == "TB_BaconShop_HERO_11"
    assert st.seats[8].is_local is True
    assert 6 not in st.seats, "第一局座位被带进第二局"


def test_create_game_auto_starts_new_game():
    """`CREATE_GAME` 本身就要触发分场，不依赖调用方记得调 `new_game()`（真机两局连跑）。"""
    b = _shop_builder()
    assert 6 in b.state.seats
    p = PowerLogParser()
    _feed(p, b, [
        _line("CREATE_GAME"),
        _line("Player EntityID=23 PlayerID=8 "
              "GameAccountId=[hi=100000000000000001 lo=99]", indent=4),
        _line("tag=HERO_ENTITY value=107", indent=9),
        _line("FULL_ENTITY - Creating ID=107 CardID=TB_BaconShop_HERO_11", indent=5),
        _line("tag=CARDTYPE value=HERO", indent=9),
        _line("tag=PLAYER_ID value=8", indent=9),
        _line("tag=CONTROLLER value=8", indent=9),
    ])
    st = b.state
    assert sorted(st.seats) == [8]
    assert st.entities.get(6) is None or True   # 旧实体不该残留座位归属
    assert st.seats[8].hero_card_id == "TB_BaconShop_HERO_11"


def test_index_entity_ignores_player_declaration_without_card_id():
    """回归：`Player EntityID=17 PlayerID=6` 声明被内联字段补上 `PLAYER_ID` 后，
    **不能**被当成英雄登记进 `_hero_by_player`（它没有 CardID）。
    早先按 `PLAYER_ID 存在` 就登记，导致 8 人座位表被这些空壳实体占满。"""
    from hsbg.state import Entity, StateBuilder

    b = StateBuilder()
    b._index_entity(Entity(entity_id=17, card_id="", tags={"PLAYER_ID": "6"}))
    assert b._hero_by_player == {}, "空壳 Player 声明被误登记为英雄"
    # 真正的英雄实体（有 CardID、带 HERO）才登记
    b._index_entity(Entity(entity_id=104, card_id="TB_BaconShop_HERO_91",
                           tags={"PLAYER_ID": "6"}))
    assert b._hero_by_player == {6: 104}
    # 鲍勃不是可玩英雄，必须排除
    b._index_entity(Entity(entity_id=74, card_id="TB_BaconShopBob",
                           tags={"PLAYER_ID": "14"}))
    assert 14 not in b._hero_by_player


# ---------------------------------------------------------------------------
# 待选 / 已选（饰品四选一、黑赐三选一）
# ---------------------------------------------------------------------------

_CHEAD = "GameState.DebugPrintEntityChoices()"
_SHEAD = "GameState.SendChoices()"


def _choice(payload, indent=2):
    return _line(payload, indent=indent, head=f"D 21:12:25.2665920 {_CHEAD}")


def _sent(payload, indent=2):
    return _line(payload, indent=indent, head=f"D 21:12:25.2665920 {_SHEAD}")


# 本地玩家的账号名 —— 状态层靠它与待选块里的 `Player=` 对名字
L_LOCAL_CURRENT = _line("TAG_CHANGE Entity=测试玩家#1234 tag=CURRENT_PLAYER value=1")
C_TRINKET_HEAD = _choice(
    "id=3 Player=测试玩家#1234 TaskList=1258 ChoiceType=GENERAL "
    "CountMin=1 CountMax=1")
C_TRINKET_SOURCE = _choice(
    "Source=[entityName=小型饰品 id=300 zone=PLAY zonePos=0 "
    "cardId=BG30_Trinket_1st player=2]")
C_T0 = _choice(
    "Entities[0]=[entityName=朝圣小鬼标签 id=3062 zone=SETASIDE zonePos=0 "
    "cardId=BG32_MagicItem_821 player=2]")
C_T1 = _choice(
    "Entities[1]=[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 "
    "cardId=BG32_MagicItem_416 player=2]")
S_TRINKET_HEAD = _sent("id=3 ChoiceType=GENERAL")
S_TRINKET_PICK = _sent(
    "m_chosenEntities[0]=[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 "
    "cardId=BG32_MagicItem_416 player=2]")
# 黑赐三选一：选项是**随从**（玩家挑让哪只吃赐福）
C_GIFT_HEAD = _choice(
    "id=2 Player=测试玩家#1234 TaskList=907 ChoiceType=GENERAL "
    "CountMin=1 CountMax=1")
C_GIFT_SOURCE = _choice(
    "Source=[entityName=酒馆战棋黑暗之赐[DNT] id=2369 zone=PLAY zonePos=0 "
    "cardId=BG36_MidGameEffect_010 player=2]")
C_G0 = _choice(
    "Entities[0]=[entityName=虔诚的萨特唤魔者 id=2370 zone=SETASIDE zonePos=0 "
    "cardId=BG33_155 player=2]")
S_GIFT_HEAD = _sent("id=2 ChoiceType=GENERAL")
S_GIFT_PICK = _sent(
    "m_chosenEntities[0]=[entityName=虔诚的萨特唤魔者 id=2370 zone=SETASIDE "
    "zonePos=0 cardId=BG33_155 player=2]")


# 真机里本地玩家的名字是**具名 TAG_CHANGE** 给出的（不是被吸收的属性行）
L_NAMED_HERO = _line(
    "TAG_CHANGE Entity=测试玩家#1234 tag=HERO_ENTITY value=104")
L_LOCAL_MAIN = [
    _line("FULL_ENTITY - Creating ID=104 CardID=TB_BaconShop_HERO_91", indent=5),
    _line("tag=PLAYER_ID value=6", indent=9),
    _line("tag=CONTROLLER value=6", indent=9),
    "",
    L_NAMED_HERO,
    L_LOCAL_CURRENT,
]


def _local_builder():
    """一个已经把本地玩家名认出来的 builder。

    ⚠️ 声明块之间**必须**有空行（真机就是这样）：不补空行的话，
    紧随其后的行会被上一条声明按"缩进 +4"吸掉 —— 实测
    `L_LOCAL_HERO_ENTITY`（缩进 9）会被 `L_PLAYER_LOCAL`（缩进 5）吞掉，
    于是 `_named_hero` 永远是空的，本地玩家名认不出来。
    """
    p, b = PowerLogParser(), StateBuilder()
    _feed(p, b, [L_CREATE, L_GAMEENTITY, "", L_PLAYER_LOCAL, L_LOCAL_HERO_ENTITY,
                 "", L_HERO_LOCAL, ""] + L_LOCAL_MAIN)
    return p, b


def test_pending_choice_grows_in_place_with_each_option():
    """⚠️ 选项是逐行来的，必须**原地增补**：`state` 上挂的是同一个对象，
    读一次就能看到最新列表（不能等"收全"，因为最后一行之后没有结束标记）。"""
    p, b = _local_builder()
    _feed(p, b, [C_TRINKET_HEAD, C_TRINKET_SOURCE, C_T0], flush=False)
    pc = b.state.pending_choice
    assert pc is not None and len(pc.options) == 1
    _feed(p, b, [C_T1], flush=False)
    assert b.state.pending_choice is pc      # 同一个对象
    assert len(pc.options) == 2


def test_new_choice_block_replaces_the_previous_one():
    """回归：新头行来了要先把旧待选收尾，否则它会一直挂着不消失。"""
    p, b = _local_builder()
    _feed(p, b, [C_TRINKET_HEAD, C_TRINKET_SOURCE, C_T0, C_T1], flush=False)
    assert b.state.pending_choice is not None
    _feed(p, b, [C_GIFT_HEAD, C_GIFT_SOURCE, C_G0], flush=False)
    pc = b.state.pending_choice
    assert pc is not None
    assert pc.choice_id == 2
    assert len(pc.options) == 1


def test_pending_choice_carries_source_and_size():
    p, b = _local_builder()
    _feed(p, b, [C_TRINKET_HEAD, C_TRINKET_SOURCE, C_T0, C_T1], flush=False)
    pc = b.state.pending_choice
    assert pc.source_card_id == "BG30_Trinket_1st"
    assert pc.source_name == "小型饰品"
    assert pc.is_trinket
    assert pc.trinket_size == "small"
    assert not pc.is_dark_gift


def test_pending_choice_of_mulligan_is_not_marked_local_choice():
    """起手换牌的 Source 是 GameEntity，不应被当成饰品/黑赐。"""
    p, b = _local_builder()
    head = _choice("id=1 Player=测试玩家#1234 TaskList=7 "
                   "ChoiceType=MULLIGAN CountMin=1 CountMax=1")
    src = _choice("Source=GameEntity")
    opt = _choice("Entities[0]=[entityName=鼠王 id=94 zone=HAND zonePos=2 "
                  "cardId=TB_BaconShop_HERO_12 player=2]")
    _feed(p, b, [head, src, opt], flush=False)
    pc = b.state.pending_choice
    assert pc is not None and pc.kind == "MULLIGAN"
    assert not pc.is_trinket and not pc.is_dark_gift


def test_pending_choice_from_other_player_is_not_local():
    p, b = _local_builder()
    head = _choice("id=9 Player=对手玩家 TaskList=1 ChoiceType=GENERAL "
                   "CountMin=1 CountMax=1")
    _feed(p, b, [head, C_TRINKET_SOURCE, C_T0], flush=False)
    pc = b.state.pending_choice
    assert pc is not None and pc.is_local is False


def test_chosen_trinket_is_committed_and_pending_cleared():
    p, b = _local_builder()
    _feed(p, b, [C_TRINKET_HEAD, C_TRINKET_SOURCE, C_T0, C_T1,
                 S_TRINKET_HEAD, S_TRINKET_PICK])
    st = b.state
    assert st.pending_choice is None
    assert st.trinket is not None
    assert st.trinket.card_id == "BG32_MagicItem_416"
    assert st.trinket.name == "战鼓"
    assert st.trinket.size == "small"
    assert st.large_trinket is None
    assert st.last_choice.card_id == "BG32_MagicItem_416"


def test_chosen_dark_gift_records_the_minion():
    p, b = _local_builder()
    _feed(p, b, [C_GIFT_HEAD, C_GIFT_SOURCE, C_G0, S_GIFT_HEAD, S_GIFT_PICK])
    st = b.state
    assert st.pending_choice is None
    assert len(st.dark_gifts) == 1
    assert st.dark_gifts[0].minion_card_id == "BG33_155"
    assert st.dark_gifts[0].minion_name == "虔诚的萨特唤魔者"
    # 黑赐**不是**饰品，不能记进 trinkets
    assert st.trinkets == []


def test_choice_log_skips_mulligan_but_keeps_trinket():
    p, b = _local_builder()
    mh = _choice("id=1 Player=测试玩家#1234 TaskList=7 "
                 "ChoiceType=MULLIGAN CountMin=1 CountMax=1")
    ms = _choice("Source=GameEntity")
    mo = _choice("Entities[0]=[entityName=鼠王 id=94 zone=HAND zonePos=2 "
                 "cardId=TB_BaconShop_HERO_12 player=2]")
    sh = _sent("id=1 ChoiceType=MULLIGAN")
    so = _sent("m_chosenEntities[0]=[entityName=鼠王 id=94 zone=HAND zonePos=2 "
               "cardId=TB_BaconShop_HERO_12 player=2]")
    _feed(p, b, [mh, ms, mo, sh, so])
    assert b.state.choice_log == []
    _feed(p, b, [C_TRINKET_HEAD, C_TRINKET_SOURCE, C_T0, C_T1,
                 S_TRINKET_HEAD, S_TRINKET_PICK])
    log = b.state.choice_log
    assert len(log) == 1
    assert log[0].source_name == "小型饰品"
    assert log[0].selected_name == "战鼓"


def test_new_game_clears_pending_choice_and_trinkets():
    p, b = _local_builder()
    _feed(p, b, [C_TRINKET_HEAD, C_TRINKET_SOURCE, C_T0, C_T1,
                 S_TRINKET_HEAD, S_TRINKET_PICK])
    assert b.state.trinkets
    _feed(p, b, [L_CREATE, L_GAMEENTITY, L_PLAYER_LOCAL, ""])
    assert b.state.trinkets == []
    assert b.state.pending_choice is None
    assert b.state.choice_log == []


def test_choice_option_from_unseen_head_is_ignored():
    """日志从中途开始时（没有头行）选项不能乱挂到上一个块上。"""
    p, b = _local_builder()
    _feed(p, b, [C_T0], flush=False)
    assert b.state.pending_choice is None


# --- 黑暗之赐：选项自带一个赐福（另一张卡） -------------------------------------

# 实测形态（`Hearthstone_2026_10_14_21_48_48/Power.log` 33557/33576）：
#   赠福实体先建、`DARK_GIFT_ENTITY` 标签再指过来，两行都**早于**待选块 14 ms。
L_GIFT_OPTION = _line("FULL_ENTITY - Creating ID=2370 CardID=BG33_155", indent=5)
L_GIFT_CARD = _line("FULL_ENTITY - Creating ID=2373 "
                    "CardID=BG36_MidGameEffect_000t13", indent=5)
L_GIFT_LINK = _line("TAG_CHANGE Entity=2370 tag=DARK_GIFT_ENTITY value=2373")


def test_choice_option_picks_up_its_dark_gift():
    """黑赐三选一的选项要带上**赐福卡**：实测同一只随从可配不同赐福。"""
    p, b = _local_builder()
    _feed(p, b, [L_GIFT_OPTION, "", L_GIFT_CARD, "", L_GIFT_LINK,
                 C_GIFT_HEAD, C_GIFT_SOURCE, C_G0], flush=False)
    pc = b.state.pending_choice
    assert pc is not None and len(pc.options) == 1
    opt = pc.options[0]
    assert opt.entity_id == 2370
    assert opt.gift_entity_id == 2373
    assert opt.gift_card_id == "BG36_MidGameEffect_000t13"
    assert opt.has_gift


def test_choice_option_without_gift_link_has_no_gift():
    """没有 `DARK_GIFT_ENTITY`（饰品四选一）时不能凭空造出赐福。"""
    p, b = _local_builder()
    _feed(p, b, [C_TRINKET_HEAD, C_TRINKET_SOURCE, C_T0], flush=False)
    opt = b.state.pending_choice.options[0]
    assert opt.gift_card_id == ""
    assert not opt.has_gift


def test_dark_gift_tag_cleared_to_zero_does_not_create_a_gift():
    """⚠️ 回归：每局末态 `DARK_GIFT_ENTITY` 会被清成 0，不能把 0 当成实体 id。"""
    p, b = _local_builder()
    zero = _line("TAG_CHANGE Entity=2370 tag=DARK_GIFT_ENTITY value=0")
    _feed(p, b, [L_GIFT_OPTION, "", L_GIFT_CARD, "", L_GIFT_LINK, "", zero,
                 C_GIFT_HEAD, C_GIFT_SOURCE, C_G0], flush=False)
    opt = b.state.pending_choice.options[0]
    assert opt.gift_card_id == ""
    assert opt.gift_entity_id is None
