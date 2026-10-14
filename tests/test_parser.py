"""解析器单元测试。

全部用**从真实日志里摘抄的行**做输入，不自己编格式——编格式容易编出
"测试通过但真机失败"的假安全感。
"""

from hsbg.events import EventType
from hsbg.parser import PowerLogParser, parse_entity_ref

# --- 真实行样例（摘自 Hearthstone_2026_10_10_21_11_09/Power.log） ---
#
# ⚠️ 缩进不要手敲空格。日志格式是 `<前缀> - <载荷>`，而 `_LINE_RE` 写成
# `\(\)\s+-\s?(?P<payload>.*)$` —— dash 之后最多吃掉 1 个空格，剩下全是载荷缩进。
# 手敲极易差一格，而差一格就会让「属性行 vs 块内裸 tag」的分派翻转（这两类只差缩进）。
# 所以统一用 _line() 生成，把缩进写成显式参数。

_HEAD = "D 21:12:25.2665920 GameState.DebugPrintPower()"
_DASH = " - "


def _line(payload: str, indent: int = 0, head: str = _HEAD) -> str:
    """构造一行日志。`indent` = **dash 之后的空格数**（取真机档位 4/8/12/16…）。

    ⚠️ 注意这不是「载荷缩进」：`_LINE_RE` 的 `-\\s?` 会吃掉其中 1 个，
    所以**载荷缩进 = indent - 1**（真机实测载荷缩进恰好是 3/7/11/15…）。
    保留这个偏移是为了让测试里的数字与真实日志直接可比。
    """
    return head + _DASH + " " * indent + payload


L_CREATE = _line("CREATE_GAME")
L_FULL = _line("FULL_ENTITY - Creating ID=73 CardID=TB_BaconShop_HERO_KelThuzad", indent=4)
# 属性行缩进必须 = 宿主缩进 + 4（L_FULL 是 4 → 这里是 8）
L_TAGPROP = _line("tag=CONTROLLER value=6", indent=8)
L_TAGCHANGE_TOP = _line("TAG_CHANGE Entity=GameEntity tag=TURN value=1")
L_TAGCHANGE_CN = _line("TAG_CHANGE Entity=调酒师鲍勃 tag=CURRENT_PLAYER value=1")
L_TAGCHANGE_INDENT = _line("TAG_CHANGE Entity=18 tag=CURRENT_PLAYER value=1", indent=4)
L_BRACKET_ENTITY = (
    "Entity=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=244 "
    "zone=SETASIDE zonePos=0 cardId= player=6]"
)
L_TAGCHANGE_BRACKET = _line(
    "TAG_CHANGE Entity=[entityName=欢悦 id=735 zone=PLAY zonePos=1 "
    "cardId=BG36_110 player=14] tag=ZONE value=PLAY",
    indent=12,
)
# 块内裸 tag：缩进 8，但当前宿主是缩进 4 的 Player 容器（4+4=8 会命中），
# 所以这个用例会先 flush 掉宿主再喂它，确保它走 BLOCK_TAG。
L_BLOCK_TAG = _line("tag=CONTROLLER value=14", indent=8)
L_INFO = _line(
    "Info[0] = [entityName=欢悦 id=312 zone=PLAY zonePos=3 cardId=BG36_110 player=14]",
    indent=16,
)
L_BLOCK_START = _line(
    "BLOCK_START BlockType=TRIGGER Entity=302 "
    "EffectCardId=System.Collections.Generic.List`1[System.String] "
    "EffectIndex=-1 Target=0 SubOption=-1 TriggerKeyword=NONE",
    indent=4,
)
L_BLOCK_END = _line("BLOCK_END")
L_PTL_DUP = _line(
    "TAG_CHANGE Entity=测试玩家#1234 tag=RESOURCES value=3",
    head="D 21:13:10.6449230 PowerTaskList.DebugPrintPower()",
)
L_DEBUGPRINTGAME = _line(
    "GameType=GT_BATTLEGROUNDS",
    head="D 21:12:25.2665920 GameState.DebugPrintGame()",
)
L_HIDE = _line(
    "HIDE_ENTITY - Entity=[entityName=3连检测玩家强化 id=76 zone=PLAY zonePos=0 "
    "cardId=TB_BaconShop_3ofKindCheck] tag=ZONE value=SETASIDE",
    indent=4,
)
L_META = _line("META_DATA - Meta=HISTORY_TARGET Data=0 InfoCount=1", indent=4)
L_SUBSPELL = _line(
    "SUB_SPELL_START - SpellPrefabGUID=Bacon_Purchase_AE_CoinThrow_Super:b574389c1",
    indent=8,
)


def _p() -> PowerLogParser:
    return PowerLogParser()


# --- 实体引用 --------------------------------------------------------------


def test_entity_ref_id_only():
    r = parse_entity_ref("17")
    assert r.entity_id == 17, r
    assert r.is_id_only
    assert not r.is_known


def test_entity_ref_bracket_unknown():
    r = parse_entity_ref(L_BRACKET_ENTITY)
    assert r.entity_id == 244, r
    assert r.zone == "SETASIDE", r
    assert r.player == 6, r
    assert r.card_id == "", r
    assert not r.is_known, "UNKNOWN ENTITY 不算已知名字"


def test_entity_ref_bracket_with_chinese_name_and_cardid():
    # ⚠️ 这是 CardID→中文名 的核心来源
    r = parse_entity_ref(
        "[entityName=欢悦 id=735 zone=PLAY zonePos=1 cardId=BG36_110 player=14]"
    )
    assert r.entity_id == 735, r
    assert r.name == "欢悦", r
    assert r.card_id == "BG36_110", r
    assert r.zone == "PLAY", r
    assert r.zone_pos == 1, r
    assert r.player == 14, r
    assert r.is_known


def test_entity_ref_chinese_name_plain():
    r = parse_entity_ref("测试玩家#1234")
    assert r.name == "测试玩家#1234", r
    assert r.entity_id is None
    assert r.is_known


def test_entity_ref_empty():
    r = parse_entity_ref("")
    assert r.entity_id is None and r.name == ""


# --- 基本事件 --------------------------------------------------------------


def test_create_game():
    evs = _p().feed_line(L_CREATE)
    assert len(evs) == 1 and evs[0].kind == EventType.CREATE_GAME, evs
    assert evs[0].timestamp == "21:12:25.2665920"


def test_full_entity_absorbs_tag_lines():
    """FULL_ENTITY 会挂起等属性行，属性行合并进 tags，不单独产出事件。"""
    p = _p()
    out = p.feed_line(L_FULL)
    assert out == [], "FULL_ENTITY 应挂起，等待后续 tag 行"
    assert p.feed_line(L_TAGPROP) == [], "属性行必须被吸收"
    assert p.feed_line(L_TAGPROP) == [], "属性行必须被吸收"
    flushed = p.flush()
    assert len(flushed) == 1, flushed
    ev = flushed[0]
    assert ev.kind == EventType.FULL_ENTITY
    assert ev.entity_id == 73
    assert ev.card_id == "TB_BaconShop_HERO_KelThuzad"
    assert ev.entity is None, "FULL_ENTITY 的 CardID 形态没有方括号实体"
    assert ev.tags == {"CONTROLLER": "6"} and ev.tags["CONTROLLER"] == "6", ev.tags


def test_tag_change_top_level():
    evs = _p().feed_line(L_TAGCHANGE_TOP)
    assert len(evs) == 1, evs
    ev = evs[0]
    assert ev.kind == EventType.TAG_CHANGE
    assert ev.tag == "TURN" and ev.value == "1"
    assert ev.entity.name == "GameEntity", ev.entity


def test_tag_change_chinese_entity():
    ev = _p().feed_line(L_TAGCHANGE_CN)[0]
    assert ev.entity.name == "调酒师鲍勃", ev.entity
    assert ev.tag == "CURRENT_PLAYER" and ev.value == "1"


def test_tag_change_indented_is_still_tag_change():
    """⚠️ 回归：块内缩进的 TAG_CHANGE 曾被整批丢进 RAW（少一个数量级）。"""
    evs = _p().feed_line(L_TAGCHANGE_INDENT)
    assert len(evs) == 1, evs
    ev = evs[0]
    assert ev.kind == EventType.TAG_CHANGE, f"缩进行被误判成 {ev.kind}"
    assert ev.entity_id == 18
    assert ev.indent == 4, ev.indent


def test_tag_change_bracket_entity_deep_indent():
    ev = _p().feed_line(L_TAGCHANGE_BRACKET)[0]
    assert ev.kind == EventType.TAG_CHANGE
    assert ev.entity.card_id == "BG36_110", ev.entity
    assert ev.indent == 12, ev.indent
    assert ev.tag == "ZONE" and ev.value == "PLAY"


def test_block_tag_without_entity():
    ev = _p().feed_line(L_BLOCK_TAG)[0]
    assert ev.kind == EventType.BLOCK_TAG, ev
    assert ev.tag == "CONTROLLER" and ev.value == "14"
    assert ev.in_block


def test_block_tag_does_not_leak_into_pending_entity():
    """块内 tag 行不能被误当成宿主实体的属性。

    同一行 `tag=CONTROLLER value=14`（缩进 8）在两种上下文里归属不同：
      - 宿主 FULL_ENTITY 缩进 4、且它还在累积 → 8 = 4+4，被吸收进宿主
      - 宿主已经 flush 掉、且深度 4 上也没有别的实体声明过 → 无从归属，只能是 BLOCK_TAG
    注意「宿主 flush 了」不等于「深度表清空」：深度表记的是该深度最近声明的实体，
    所以这里要显式清空，才能构造出真正的「无主」边界。
    """
    p = _p()
    p.feed_line(L_FULL)
    p.feed_line("")  # 空行把 FULL_ENTITY 交出去，宿主出栈
    p._entity_by_indent.clear()  # 深度 4 上也没有实体了
    p._parent_owner_id = None    # 也清掉「最近头命令」这条退路 → 彻底无主
    evs = p.feed_line(L_BLOCK_TAG)
    assert [e.kind for e in evs] == [EventType.BLOCK_TAG], evs
    assert evs[0].tag == "CONTROLLER" and evs[0].value == "14"


def test_block_tag_is_absorbed_when_indent_matches_host_plus_four():
    """反向用例：缩进命中 宿主+4 时必须被吸收，而不是漏成 BLOCK_TAG。"""
    p = _p()
    p.feed_line(L_FULL)  # 宿主缩进 4
    assert p.feed_line(L_TAGPROP) == [], "缩进 8 = 4+4，必须被吸收"
    flushed = p.flush()
    assert len(flushed) == 1, flushed
    assert flushed[0].kind == EventType.FULL_ENTITY
    assert flushed[0].tags == {"CONTROLLER": "6"}, flushed[0].tags


def test_block_tag_owner_is_entity_declared_at_indent_minus_four():
    """裸 tag 行的 owner = 「缩进 − 4」深度上最近声明的实体。

    ⚠️ 这一条是回归测试：早期版本把 owner 取成「最近一条头命令的 owner」，
    于是 BLOCK_START（其 Entity= 是触发者，不是被改属性的实体）后面的属性
    全部挂错实体 —— 实测 766 条 ATTACHED_TAG 无一例外挂错。深度表才对得上。
    """
    p = _p()
    # 深度 4 声明实体 302（BLOCK_START 自己的 Entity 是深度 4 的另一个 id）
    p.feed_line(_line("FULL_ENTITY - Creating ID=302 CardID=BG36_110", indent=4))
    p.feed_line("")
    p.feed_line(_line("BLOCK_START BlockType=TRIGGER Entity=999", indent=4))
    ev = p.feed_line(_line("tag=CONTROLLER value=9", indent=8))[0]
    assert ev.kind == EventType.ATTACHED_TAG, ev
    assert ev.owner_id == 302, ev.owner_id
    assert ev.parent_cmd == "BLOCK_START", ev.parent_cmd
    assert ev.parent_indent == 4, ev.parent_indent



def test_info_line_carries_cardid_and_name():
    ev = _p().feed_line(L_INFO)[0]
    assert ev.kind == EventType.INFO, ev
    assert ev.entity.card_id == "BG36_110", ev.entity
    assert ev.entity.name == "欢悦", ev.entity
    assert ev.entity.zone == "PLAY" and ev.entity.zone_pos == 3, ev.entity


def test_block_start_fields():
    ev = _p().feed_line(L_BLOCK_START)[0]
    assert ev.kind == EventType.BLOCK_START, ev
    b = ev.block
    assert b.block_type == "TRIGGER", b
    assert b.entity_id == 302, b
    assert b.target == 0 and b.sub_option == -1, b
    assert b.trigger_keyword == "NONE", b


def test_block_end_without_params():
    ev = _p().feed_line(L_BLOCK_END)[0]
    assert ev.kind == EventType.BLOCK_END, ev
    assert ev.block.block_type == "", ev.block


def test_hide_entity():
    ev = _p().feed_line(L_HIDE)[0]
    assert ev.kind == EventType.HIDE_ENTITY, ev
    assert ev.entity.card_id == "TB_BaconShop_3ofKindCheck", ev.entity
    assert ev.entity.name == "3连检测玩家强化", ev.entity
    assert ev.tag == "ZONE" and ev.value == "SETASIDE"


def test_meta_data():
    ev = _p().feed_line(L_META)[0]
    assert ev.kind == EventType.META_DATA, ev
    assert ev.meta == "HISTORY_TARGET", ev
    assert ev.meta_count == 1, ev


def test_subspell_is_silently_dropped():
    """SUB_SPELL_* 是已知噪声，不该占事件流。"""
    p = _p()
    assert p.feed_line(L_SUBSPELL) == []


# --- 来源过滤 --------------------------------------------------------------


def test_powertasklist_duplicate_filtered_by_default():
    """⚠️ PowerTaskList 与 GameState 各打一遍，默认只取一份，否则事件翻倍。"""
    p = _p()
    assert p.feed_line(L_PTL_DUP) == [], "PowerTaskList 分支应被过滤"
    assert p.stats.lines_power == 0


def test_debugprintgame_ignored_even_if_requested():
    p = PowerLogParser(sources=("GameState.DebugPrintPower", "GameState.DebugPrintGame"))
    assert p.feed_line(L_DEBUGPRINTGAME) == []


def test_powertasklist_can_be_enabled_explicitly():
    p = PowerLogParser(sources=("PowerTaskList.DebugPrintPower",))
    evs = p.feed_line(L_PTL_DUP)
    assert len(evs) == 1, evs
    assert evs[0].entity.name == "测试玩家#1234", evs[0].entity


# --- 容错 ------------------------------------------------------------------


def test_blank_line_flushes_pending():
    p = _p()
    p.feed_line(L_FULL)
    out = p.feed_line("")
    assert len(out) == 1 and out[0].kind == EventType.FULL_ENTITY, out


def test_unparseable_line_does_not_crash():
    p = _p()
    out = p.feed_line("这不是一行日志")
    assert out == []
    assert p.stats.unknown_payloads >= 0


def test_empty_value_becomes_zero():
    ev = _p().feed_line(
        "D 21:13:10.6449230 GameState.DebugPrintPower() - TAG_CHANGE Entity=17 tag=EXHAUSTED value="
    )[0]
    assert ev.value == "0", ev.value


def test_line_numbers_are_sequential():
    p = _p()
    p.feed_line(L_CREATE)
    p.feed_line(L_TAGCHANGE_TOP)
    assert p.stats.lines_total == 2
    assert p.stats.lines_power == 2


def test_feed_multiline():
    p = _p()
    evs = p.feed(L_CREATE + "\n" + L_TAGCHANGE_TOP + "\n" + L_TAGCHANGE_CN + "\n")
    assert len(evs) == 3, evs
    assert [e.kind for e in evs] == [
        EventType.CREATE_GAME,
        EventType.TAG_CHANGE,
        EventType.TAG_CHANGE,
    ]


# ---------------------------------------------------------------------------
# `CREATE_GAME` 块里的实体声明（P0 收官时补的第三个语法形态）
#
# ⚠️ 这 340 行（GameEntity 2 + Player 338）既没有 `ID=`，也没有 `-` 分隔，
# 是 `PLAYER_ID` / `HERO_ENTITY` 的唯一来源。漏掉它们 → P1 重建不出 8 人座位。
#
# ⚠️ 缩进档位按**真机**取值：真机日志里声明是「dash 后 5 个空格」，它的属性行是
# 「dash 后 9 个空格」。`_line(indent=N)` 的 N 就是"dash 后空格数"，所以
# `indent=5` + `indent=9` 才是真机形态。解析器给声明设的吸收锚点是
# `indent - 4 = 1`，属性行 `9 == 1 + 4 + 4`（锚点 + `_TAG_INDENT_STEP` 的
# 相对父命令偏移）才能被接住。
# ---------------------------------------------------------------------------

L_GAMEENTITY = _line("GameEntity EntityID=16", indent=5)
L_GAMEENTITY_TAG = _line("tag=ENTITY_ID value=16", indent=9)
L_PLAYER_DECL = _line(
    "Player EntityID=17 PlayerID=6 GameAccountId=[hi=100000000000000001 lo=200000001]", indent=5
)
L_PLAYER_TAG = _line("tag=PLAYER_ID value=6", indent=9)


def test_player_declaration_is_recognized():
    """声明的产出时机与 FULL_ENTITY 一致：等它自己的属性行吸收完再交出。"""
    p = _p()
    p.feed_line(L_CREATE)
    assert p.feed_line(L_GAMEENTITY) == []      # 先入 pending，吸收它的属性
    p.feed_line(L_GAMEENTITY_TAG)
    evs = p.flush()                             # 流结束 → 交出声明
    assert [e.kind for e in evs] == [EventType.PLAYER], evs
    assert evs[0].entity_id == 16
    assert evs[0].detail.startswith("GameEntity EntityID=16")


def test_player_declaration_absorbs_its_tag_lines():
    """声明的属性行（缩进 = 声明缩进 + 4）要挂到声明实体上。"""
    p = _p()
    p.feed_line(L_CREATE)
    p.feed_line(L_GAMEENTITY)              # 锚点落在声明缩进上
    p.feed_line(L_GAMEENTITY_TAG)          # 缩进 = 锚点 + 4 → 被吸收
    assert p.stats.lines_power == 3
    # 声明 + 属性行只在 flush 时交出一个事件（属性行不额外产出）
    evs = p.flush()
    assert [e.kind for e in evs] == [EventType.PLAYER], evs
    assert evs[0].entity_id == 16


def test_player_declaration_tags_are_absorbed():
    """吸收本身要落进宿主 tags —— 用 flush 后的实体事件检查。"""
    p = _p()
    p.feed_line(L_CREATE)
    p.feed_line(L_GAMEENTITY)
    p.feed_line(L_GAMEENTITY_TAG)
    evs = p.flush()
    # 属性行被吸收 = 该行没有独立事件；宿主 id 与 ENTITY_ID 一致
    assert evs[0].tags.get("ENTITY_ID") == "16", evs[0].tags
    assert p.stats.absorbed_id_mismatch == 0
    assert p.stats.absorbed_checked == 1


def test_player_id_is_absorbed_into_declaration():
    """`tag=PLAYER_ID` 归属靠缩进，不靠 `ENTITY_ID` 自证。

    ⚠️ 这里**不**喂中间那条 GameEntity 声明：`_pending` 的锚点只记缩进，
    会按位置把 Player 声明吞掉（真机日志里声明与自己属性行是紧邻的，
    352 行属性全部紧跟各自声明，不存在插队）。
    """
    p = _p()
    p.feed_line(L_CREATE)
    p.feed_line(L_PLAYER_DECL)
    p.feed_line(L_PLAYER_TAG)              # 缩进 9 = 声明缩进 5 + 4 → 被吸收
    evs = p.flush()
    assert [e.kind for e in evs] == [EventType.PLAYER], evs
    assert evs[0].entity_id == 17
    assert evs[0].tags.get("PLAYER_ID") == "6", evs[0].tags
    # PLAYER_ID 不是 ENTITY_ID，不参与自证计数
    assert p.stats.absorbed_checked == 0


def test_create_game_block_tags_are_not_unowned():
    """回归：声明被识别前，这批属性行会退化成无主 BLOCK_TAG（实测 88 行）。"""
    p = _p()
    p.feed_line(L_CREATE)
    p.feed_line(L_GAMEENTITY)
    p.feed_line(L_GAMEENTITY_TAG)
    p.flush()
    assert p.stats.unowned_tags == 0


# ---------------------------------------------------------------------------
# 待选 / 已选（饰品四选一、黑赐三选一、起手换牌）
#
# ⚠️ 这两条流只在 `GameState` 分支下有，`PowerTaskList` 没有对应输出，
# 所以 DEFAULT_SOURCES 里必须带上它们，且不能用 `GameState.DebugPrintPower` 的头。
# ---------------------------------------------------------------------------

_CHOICE_HEAD = "D 21:12:25.2665920 GameState.DebugPrintEntityChoices()"
_SENT_HEAD = "D 21:12:25.2665920 GameState.SendChoices()"


def _choice_line(payload: str, indent: int = 2) -> str:
    return _line(payload, indent=indent, head=_CHOICE_HEAD)


def _sent_line(payload: str, indent: int = 2) -> str:
    return _line(payload, indent=indent, head=_SENT_HEAD)


C_HEAD = _choice_line(
    "id=3 Player=测试玩家#1234 TaskList=1258 ChoiceType=GENERAL "
    "CountMin=1 CountMax=1"
)
C_SOURCE = _choice_line(
    "Source=[entityName=小型饰品 id=300 zone=PLAY zonePos=0 "
    "cardId=BG30_Trinket_1st player=2]"
)
C_OPT0 = _choice_line(
    "Entities[0]=[entityName=朝圣小鬼标签 id=3062 zone=SETASIDE zonePos=0 "
    "cardId=BG32_MagicItem_821 player=2]"
)
C_OPT1 = _choice_line(
    "Entities[1]=[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 "
    "cardId=BG32_MagicItem_416 player=2]"
)
S_HEAD = _sent_line("id=3 ChoiceType=GENERAL")
S_OPT = _sent_line(
    "m_chosenEntities[0]=[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 "
    "cardId=BG32_MagicItem_416 player=2]"
)
MULLIGAN_HEAD = _choice_line(
    "id=1 Player=测试玩家#1234 TaskList=7 ChoiceType=MULLIGAN "
    "CountMin=1 CountMax=1"
)
MULLIGAN_SOURCE = _choice_line("Source=GameEntity")


def test_choice_streams_are_in_default_sources():
    """⚠️ 不接这两条流的话饰品/黑赐在盘面上完全看不见（当初就是这样漏的）。"""
    from hsbg.parser import DEFAULT_SOURCES
    assert "GameState.DebugPrintEntityChoices" in DEFAULT_SOURCES
    assert "GameState.SendChoices" in DEFAULT_SOURCES


def test_choice_head_emits_id_kind_and_player():
    ev = _p().feed_line(C_HEAD)[0]
    assert ev.kind == EventType.CHOICE
    assert ev.choice_id == 3
    assert ev.choice_kind == "GENERAL"
    assert ev.choice_player == "测试玩家#1234"
    assert ev.choice_count == 1
    assert ev.choice_option is None


def test_choice_source_line_emits_source_text():
    ev = _p().feed_line(C_SOURCE)[0]
    assert ev.kind == EventType.CHOICE
    # ⚠️ Source= 行**不带 id**，所以 choice_id 必须是 None
    assert ev.choice_id is None
    assert "BG30_Trinket_1st" in ev.choice_source


def test_choice_option_line_has_no_id_but_carries_the_option():
    ev = _p().feed_line(C_OPT0)[0]
    assert ev.kind == EventType.CHOICE
    assert ev.choice_id is None
    assert ev.choice_option.index == 0
    assert ev.choice_option.card_id == "BG32_MagicItem_821"
    assert ev.choice_option.entity_id == 3062
    assert ev.choice_option.name == "朝圣小鬼标签"
    assert ev.choice_option.is_known


def test_choice_option_is_not_treated_as_tag_line():
    """回归：选项行是缩进行，若不先分派会去匹配 `_TAG_LINE_RE` 落成 RAW。"""
    p = _p()
    evs = p.feed_line(C_OPT1)
    assert [e.kind for e in evs] == [EventType.CHOICE]
    assert p.stats.by_kind.get("RAW", 0) == 0


def test_choice_head_does_not_flush_pending_entity():
    """⚠️ 待选块紧跟在 TAG_CHANGE 之后时，提前 flush 会截断还没吸完属性的实体。"""
    p = _p()
    p.feed_line(L_FULL)
    p.feed_line(L_TAGPROP)
    out = p.feed_line(C_HEAD)
    assert [e.kind for e in out] == [EventType.CHOICE]
    # 属性行仍留在 pending 实体上，flush 时一起出来
    evs = p.flush()
    assert evs and evs[0].tags.get("CONTROLLER") == "6"


def test_mulligan_source_is_game_entity():
    ev = _p().feed_line(MULLIGAN_SOURCE)[0]
    assert ev.choice_source == "GameEntity"
    ev2 = _p().feed_line(MULLIGAN_HEAD)[0]
    assert ev2.choice_kind == "MULLIGAN"


def test_sent_choices_head_emits_result():
    ev = _p().feed_line(S_HEAD)[0]
    assert ev.kind == EventType.CHOICE_RESULT
    assert ev.choice_id == 3
    assert ev.choice_kind == "GENERAL"


def test_sent_choices_option_carries_the_pick():
    ev = _p().feed_line(S_OPT)[0]
    assert ev.kind == EventType.CHOICE_RESULT
    assert ev.choice_option.card_id == "BG32_MagicItem_416"
    assert ev.choice_option.name == "战鼓"


def test_choice_lines_do_not_leak_into_power_task_list():
    """`PowerTaskList` 那份重复输出里没有待选块，喂进去应当被来源过滤丢掉。"""
    p = _p()
    line = _line(C_HEAD.split(_DASH, 1)[1], indent=2,
                 head="D 21:12:25.2665920 PowerTaskList.DebugPrintPower()")
    assert p.feed_line(line) == []


def test_parse_source_ref_strips_prefix():
    from hsbg.parser import parse_source_ref
    ref = parse_source_ref(
        "[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 "
        "cardId=BG32_MagicItem_416 player=2]"
    )
    assert ref.card_id == "BG32_MagicItem_416"
    assert ref.entity_id == 3063
    # `Source=GameEntity` 也容忍
    ref2 = parse_source_ref("GameEntity")
    assert ref2.name == "GameEntity"
