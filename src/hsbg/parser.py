"""Power.log 流式解析器（P0）。

职责：把 `Power.log` 的一行文本变成 `PowerEvent`。**不含任何状态**。

实测的日志行格式（2026-10-13，本机）：

    D 21:12:25.2665920 GameState.DebugPrintPower() - CREATE_GAME
    D 21:12:25.2665920 GameState.DebugPrintPower() -     tag=CONTROLLER value=6
    D 21:12:25.2665920 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1
    D 21:12:25.2665920 PowerTaskList.DebugPrintPower() - FULL_ENTITY - Creating ID=35 CardID=TB_BaconShop_HERO_PH
    D 21:12:25.2665920 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS

三个关键点（都踩过）：

1. **`GameState.DebugPrintPower()` 与 `PowerTaskList.DebugPrintPower()` 会把同一动作各打一遍**
   （实测 344,567 vs 334,597 行）。默认只取 `GameState` 一份，否则事件翻倍。
   需要实体名时再按流筛选（`PowerTaskList` 分支的 `Entity=` 带中文名）。
2. **命令词可以带缩进**。`TAG_CHANGE` / `FULL_ENTITY` / `BLOCK_START` 大量出现在
   块内部，带 4–20 个空格。⚠️ 早期版本用 `^TAG_CHANGE` 锚定行首，把 129,559 行
   全部丢进 RAW，TAG_CHANGE 少了一个数量级（6,999 → 146,027）。见 `_CMD_RE`。
3. **三形态实体引用**：纯 ID / 方括号详细形态 / 中文名。方括号形态顺带有
   `zone`/`cardId`/`player` 快照，是修复实体表的免费信息源。

**意外的收获：日志里其实有卡牌中文名。**

`tag=NAME` 确实一次都没有（那是 P0 之前的结论），但方括号实体形态和
`META_DATA` 的 `Info[i]` 明细里直接写着本地化名字，且**和 CardID 挨着**：

    Info[0] = [entityName=欢悦 id=312 zone=PLAY zonePos=3 cardId=BG36_110 player=14]
    HIDE_ENTITY - Entity=[entityName=3连检测玩家强化 id=76 ... cardId=TB_BaconShop_3ofKindC...]

实测一份 92 MB 日志能抠出 **496 条 `CardID → 中文名`**。
这大幅降低 FR-3 卡牌数据层的风险（R2）——核心战棋卡可以自举，外挂库只用于补全。

**关于 `BLOCK_TAG`（块内 tag 行）：**

块内部的属性修改长这样（无实体前缀，靠缩进归属）::

    BLOCK_START BlockType=TRIGGER Entity=302 ...
            tag=CONTROLLER value=14
    BLOCK_END

实测 115,019 行。它们**不带实体 ID**，归属只能靠"当前所在块"推断，
所以事件层原样交出（`EventType.BLOCK_TAG` + `in_block=True`），
由 P1 的状态重建层负责结合块上下文归属。⚠️ 漏掉这批会导致盘面漏更新
（随从进出场、三连合成多发生在块内）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, IO, Iterator, List, Optional, Tuple

from .events import BlockRef, ChoiceOption, EventType, EntityRef, ParseStats, PowerEvent

# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

#: 日志前缀：`D 21:12:25.2665920 GameState.DebugPrintPower() - <载荷>`
#: 载荷前**恰好一个空格**，其余缩进属于载荷本身。
#: ⚠️ 载荷开头允许有缩进（不 strip），缩进是下面识别块的依据。
_LINE_RE = re.compile(
    r"^(?P<level>[A-Z])\s+"
    r"(?P<ts>\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<source>[A-Za-z_.]+)\(\)\s+-\s?"
    r"(?P<payload>.*)$"
)

#: 匹配载荷里的命令词，**允许前导缩进**。
#:
#: ⚠️ 这是实机跑出来的关键修正：`TAG_CHANGE` / `FULL_ENTITY` / `BLOCK_START`
#: 大量出现在**块内部**，带 4/8/12… 个空格缩进。实测 92 MB 日志里
#: 缩进过的 `TAG_CHANGE` 有 129,559 行（ind=4: 80,788、ind=8: 44,771、
#: ind=12: 7,300、ind=16: 4,182、ind=20: 1,295…）。早期版本用 `^TAG_CHANGE`
#: 锚定行首，把这些全丢进 RAW，导致事件数少了一个数量级。
_CMD_RE = re.compile(
    r"^\s*(?P<cmd>"
    r"TAG_CHANGE|FULL_ENTITY|SHOW_ENTITY|CHANGE_ENTITY|HIDE_ENTITY|"
    r"BLOCK_START|BLOCK_END|META_DATA|CREATE_GAME|PLAYER|TURN|GAME_ENTITY|"
    # ⚠️ `CREATE_GAME` 块里的实体声明（实测 340 行）用的是**第三种语法**：
    #     GameEntity EntityID=16
    #     Player EntityID=17 PlayerID=6 GameAccountId=[hi=... lo=...]
    # 既没有 `ID=`，也没有 `-` 分隔。必须在这里就认出来，否则 `_cmd_of` 返回
    # None，这 340 行会带着它们 352 条属性行一起落进 BLOCK_TAG —— 后果是
    # P1 重建 8 人座位时拿不到 PLAYER_ID / HERO_ENTITY。
    r"GameEntity|Player"
    r")\b"
)

#: 实体属性行相对宿主命令的缩进增量。实测恒为 4（见 tools/probe_tag_dispatch.py：
#: 宿主缩进 0→属性 4、宿主 4→属性 8、宿主 8→属性 12…）。
_TAG_INDENT_STEP = 4

#: `CREATE_GAME` 块里的实体声明行：`    GameEntity EntityID=16` / `    Player EntityID=17 PlayerID=6 ...`
#:
#: ⚠️ 这两类行没有 `ID=` 形态，实测一份 92 MB 日志里共 340 行（GameEntity 2 + Player 338），
#: 它们各自带 67 / 60 条缩进 tag 属性行。早期版本没有这条正则，这 340 行连属性一起
#: 落进 `BLOCK_TAG`，导致 CREATE_GAME 块里的 GameEntity / Player 属性（含 PLAYER_ID、
#: HERO_ENTITY、BACON_MAX_PLAYER_TECH_LEVEL）在 P1 还原时全丢。
_DECL_RE = re.compile(
    r"^(?:GameEntity|Player)\s+EntityID=(?P<eid>\d+)", re.IGNORECASE
)

#: 声明行上的内联字段：`Player EntityID=17 PlayerID=6 GameAccountId=[hi=.. lo=..]`
#:
#: ⚠️ 这些字段与 `tag=X value=Y` 形态不同，是 `KEY=VALUE` 直接跟在声明后面，
#: 不会被 `_TAG_LINE_RE` 接住。`PLAYER_ID` 正是靠这里落到实体上的（P1 的 8 人
#: 座位全靠它）；漏掉的话 `Player EntityID=17` 上永远没有 PLAYER_ID。
_DECL_FIELD_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z_0-9]*)=(?P<val>\S+)")

#: 声明行内联字段的 key 归一：`PlayerID` → `PLAYER_ID`、`PlayerTechLevel` → `PLAYER_TECH_LEVEL`。
#:
#: 全项目其余地方（`FULL_ENTITY` 的属性行、`TAG_CHANGE`）用的都是日志原生的
#: 全大写 + 下划线形态（`PLAYER_ID`）。不归一会产生两套键，P1 的座位表就查不到人。
_SNAKE_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake_tag(key: str) -> str:
    """`PlayerID` → `PLAYER_ID`；已是 `PLAYER_ID` 的保持不变。"""
    return _SNAKE_RE.sub("_", key).upper()

#: 实体属性行：`     tag=ZONE value=PLAY`
#:
#: ⚠️ 必须要求前导空白，**不能**写成 `^\s*tag=`。实测「dash 后空格数」只有
#: 4/8/12/16/20/24/28 这几档，`_LINE_RE` 的 `-\s?` 吃掉一个之后，载荷缩进
#: 是 3/7/11/15/… 全部 ≥3，没有顶格形态。若允许顶格，块内裸 tag 行会被
#: 判定为「正在累积实体的属性」，实测 BLOCK_TAG 从 115,019 崩到 854。
#: 顶格形态只在**没有** pending 实体时才出现（见下文 `_INFO_TAG_LINE_RE`）。
_TAG_LINE_RE = re.compile(r"^\s+tag=(?P<tag>\S+)(?:\s+value=(?P<value>.*))?$")

#: `FULL_ENTITY - Creating ID=35 CardID=TB_BaconShop_HERO_PH`
_FULL_ENTITY_RE = re.compile(
    r"FULL_ENTITY\s+-\s+(?P<verb>Creating|Updating)\s+"
    r"(?:(?P<entity>\[[^\]]*(?:\][^\]]*)*\])\s+)?"
    r"ID=(?P<id>\d+)(?:\s+CardID=(?P<cardid>\S*))?"
)

#: `HIDE_ENTITY - Entity=[entityName=... id=76 ...] tag=ZONE value=SETASIDE`
_HIDE_ENTITY_RE = re.compile(
    r"HIDE_ENTITY\s+-\s+Entity=(?P<entity>.*?)(?:\s+tag=(?P<tag>\S+)"
    r"(?:\s+value=(?P<value>.*))?)?$"
)

#: `META_DATA - Meta=HISTORY_TARGET Data=0 InfoCount=1`
_META_DATA_RE = re.compile(
    r"META_DATA\s+-\s+Meta=(?P<meta>\S+)(?:\s+Data=(?P<data>-?\d+))?"
    r"(?:\s+InfoCount=(?P<count>-?\d+))?"
)

#: `Info[0] = [entityName=欢悦 id=312 zone=PLAY zonePos=3 cardId=BG36_110 player=14]`
#: 或 `Info[0] = 315`。⚠️ 这是**卡牌中文名的黄金来源**：直连 CardID。
_INFO_LINE_RE = re.compile(r"^Info\[(?P<idx>\d+)\]\s*=\s*(?P<value>.*)$")

#: `tag=CONTROLLER value=6` / `tag=ZONE value=SETASIDE`
_INFO_TAG_LINE_RE = re.compile(r"^tag=(?P<tag>\S+)(?:\s+value=(?P<value>.*))?$")

#: `SHOW_ENTITY - Updating Entity=[...] CardID=...`
_SHOW_ENTITY_RE = re.compile(
    r"SHOW_ENTITY\s+-\s+Updating\s+Entity=(?P<entity>.*?)\s+CardID=(?P<cardid>\S*)"
)

#: `CHANGE_ENTITY - Updating Entity=... CardID=...`
_CHANGE_ENTITY_RE = re.compile(
    r"CHANGE_ENTITY\s+-\s+Updating\s+Entity=(?P<entity>.*?)\s+CardID=(?P<cardid>\S*)"
)

#: `TAG_CHANGE Entity=17 tag=ZONE value=PLAY`
_TAG_CHANGE_RE = re.compile(
    r"TAG_CHANGE\s+Entity=(?P<entity>.*?)\s+tag=(?P<tag>\S+)"
    r"(?:\s+value=(?P<value>.*))?$"
)

#: `BLOCK_START BlockType=TRIGGER Entity=... EffectCardId=... EffectIndex=-1 Target=0 SubOption=-1 TriggerKeyword=...`
#: `BLOCK_END` 常无任何参数，故 BlockType 及之后全部可选。
_BLOCK_RE = re.compile(
    r"^(?P<kind>BLOCK_START|BLOCK_END)"
    r"(?:\s+BlockType=(?P<btype>\S+))?"
    r"(?:\s+Entity=(?P<entity>.*?))?"
    r"(?:\s+EffectCardId=(?P<effect>.*?))?"
    r"(?:\s+EffectIndex=(?P<eidx>-?\d+))?"
    r"(?:\s+Target=(?P<target>-?\d+))?"
    r"(?:\s+SubOption=(?P<sub>-?\d+))?"
    r"(?:\s+TriggerKeyword=(?P<trigger>\S+))?$"
)

#: 方括号详细实体形态里的 `id=244` / `zone=SETASIDE` / `cardId=` / `player=6`
_BRACKET_FIELD_RE = re.compile(r"(?P<key>entityName|id|zone|zonePos|cardId|player)=([^\s\]]*)")

#: `GameState.DebugPrintEntityChoices()` 的头行：
#: `id=3 Player=测试玩家#1234 TaskList=1258 ChoiceType=GENERAL CountMin=1 CountMax=1`
_CHOICE_HEAD_RE = re.compile(
    r"^id=(?P<id>\d+)\s+Player=(?P<player>.*?)\s+TaskList=(?P<task>\S+)"
    r"\s+ChoiceType=(?P<kind>\S+)(?:\s+CountMin=(?P<cmin>\d+)\s+CountMax=(?P<cmax>\d+))?"
)

#: `  Source=[entityName=小型饰品 id=300 … cardId=BG30_Trinket_1st player=2]` / `Source=GameEntity`
_CHOICE_SOURCE_RE = re.compile(r"^Source=(?P<src>.*)$")

#: `  Entities[1]=[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 cardId=BG32_MagicItem_416 player=2]`
_CHOICE_ENTITY_RE = re.compile(r"^Entities\[(?P<idx>\d+)\]=(?P<ent>.*)$")

#: `GameState.SendChoices()` 的头行：`id=3 ChoiceType=GENERAL`
_SENT_HEAD_RE = re.compile(r"^id=(?P<id>\d+)\s+ChoiceType=(?P<kind>\S+)$")

#: `  m_chosenEntities[0]=[entityName=战鼓 id=3063 … cardId=BG32_MagicItem_416 player=2]`
_CHOSEN_ENTITY_RE = re.compile(r"^m_chosenEntities\[(?P<idx>\d+)\]=(?P<ent>.*)$")

#: `id=2 Player=测试玩家#1234 EntitiesCount=1`（DebugPrintEntitiesChosen 的头行）
_CHOSEN_HEAD_RE = re.compile(r"^id=(?P<id>\d+)\s+Player=(?P<player>.*?)\s+EntitiesCount=(?P<n>\d+)$")

#: 可选的来源过滤。默认要 GameState 分支下的三条流：
#:   - `DebugPrintPower`   —— 主盘面流（避开 PowerTaskList 的重复输出）
#:   - `DebugPrintEntityChoices` / `SendChoices` —— 待选与已选
#:     ⚠️ 这两条**只在 GameState 分支**有，PowerTaskList 没有对应输出，不会重复。
#:     不接它们的话「饰品四选一 / 黑赐三选一 / 起手换牌」在盘面上完全看不见。
DEFAULT_SOURCES = (
    "GameState.DebugPrintPower",
    "GameState.DebugPrintEntityChoices",
    "GameState.SendChoices",
)

#: GameState 分支里必须跳过的非 Power 来源（同目录下别的日志混进来时用）。
_IGNORED_SOURCES = ("GameState.DebugPrintGame",)


# ---------------------------------------------------------------------------
# 实体引用解析
# ---------------------------------------------------------------------------


def _cmd_of(payload: str) -> Optional[str]:
    """取出载荷的命令词（容忍前导缩进）。不是命令词则返回 None。

    为什么需要它：块内部的 `TAG_CHANGE` 带 4–20 个空格缩进（实测 129,559 行），
    还有 `     tag=X value=Y` 这样的裸属性行。先判命令词再分派，
    比让每个正则各自容忍缩进更清楚，也避免把裸 tag 行误当命令。
    """
    m = _CMD_RE.match(payload)
    return m.group("cmd") if m else None


def parse_source_ref(raw: str) -> EntityRef:
    """解析 `Source=[entityName=小型饰品 id=300 zone=PLAY … cardId=BG30_Trinket_1st …]`。

    `parse_entity_ref` 只认 `Entity=` 前缀或裸方括号，不认 `Source=`，
    所以这里剥掉前缀再复用。`Source=GameEntity` 也容忍（返回纯名字引用）。
    """
    body = raw.strip()
    if body.startswith("Source="):
        body = body[len("Source="):].strip()
    return parse_entity_ref(body)


def parse_entity_ref(raw: str) -> EntityRef:
    """把日志里的实体引用归一化。

    支持三种形态（实测）::

        "17"                          -> entity_id=17
        "[entityName=... id=244 ...]" -> 解析出 id/zone/cardId/player 快照
        "调酒师鲍勃"                   -> name

    另外容忍调用方把 `Entity=` 前缀一起传进来（日志里的常见形态），
    这里统一剥掉，免得每个调用点都要记着剥。
    """
    raw = (raw or "").strip()
    if raw.startswith("Entity="):
        raw = raw[len("Entity=") :].strip()
    ref = EntityRef(raw=raw)

    if not raw:
        return ref

    if raw.isdigit():
        ref.entity_id = int(raw)
        return ref

    if raw.startswith("["):
        inner = raw.strip("[]")
        # entityName 可能自带方括号（`UNKNOWN ENTITY [cardType=INVALID]`），
        # 所以先单独抠出来，其余字段再按 key= 扫。
        m = re.search(r"entityName=(.*?)\s+(?:id|zone|zonePos|cardId|player)=", inner)
        if m:
            ref.name = m.group(1).strip()
        for fm in _BRACKET_FIELD_RE.finditer(inner):
            key, val = fm.group("key"), fm.group(2)
            if key == "id" and val.isdigit():
                ref.entity_id = int(val)
            elif key == "zone":
                ref.zone = val
            elif key == "zonePos" and val.isdigit():
                ref.zone_pos = int(val)
            elif key == "cardId":
                ref.card_id = val
            elif key == "player" and val.isdigit():
                ref.player = int(val)
        # cardType 在 entityName 里，形如 UNKNOWN ENTITY [cardType=INVALID]
        cm = re.search(r"\[cardType=(\w+)\]", ref.name)
        if cm:
            ref.card_type = cm.group(1)
        return ref

    ref.name = raw
    return ref


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


class PowerLogParser:
    """无状态的增量解析器。

    用法::

        parser = PowerLogParser()
        for line in open(path):
            for ev in parser.feed_line(line):
                ...

    也可以直接 `parser.feed(text)` 喂一段多行文本。
    """

    def __init__(self, sources: Tuple[str, ...] = DEFAULT_SOURCES) -> None:
        self.sources = sources
        self.stats = ParseStats()
        self._line_no = 0
        self._pending: Optional[PowerEvent] = None
        #: `_pending` 这条实体事件的缩进锚点。
        #:
        #: ⚠️ 吸收属性行不能只看「比最近一条头命令深 4」，而要看「比**正在累积的实体**
        #: 深 4」。否则 CREATE_GAME（缩进 0）之后的 GameEntity 声明（缩进 4）会被
        #: 当成它的属性吞掉；实测正是这样丢掉了 4 条 PLAYER 声明。用 `_parent_indent`
        #: 也不行，它是「最近头命令」的缩进，可能与 `_pending` 不同源。
        self._pending_anchor_indent: int = -1

        #: 最近一条**头命令**（实体/块/玩家等）的缩进与身份，tag 行靠它定归属
        self._parent_cmd: str = ""
        self._parent_indent: int = -1
        self._parent_owner_id: Optional[int] = None

        #: 缩进深度 → 该深度上最近一次声明的实体 id。
        #:
        #: 实测归属规律比「宿主 +4」更简单：**缩进 k 的裸 tag 行，属于缩进 k-4 处
        #: 最近声明的那个实体**。宿主命令自己的 `Entity=` 是「谁触发了这个块」，
        #: 不等于属性属于谁 —— 例如
        #: `BLOCK_START ... Entity=[... id=75 ...] cardId=TB_BaconShop_8P_PlayerE`
        #: 之后紧跟的裸 tag 行（真实样例里是 `FULL_ENTITY - Creating ID=244` 那一批）
        #: 描述的是块内实体，不是 75。早期只记「最近头命令的 owner」，实测 766 条
        #: ATTACHED_TAG 全部挂错实体；改成本表后按深度取，归属才对得上。
        self._entity_by_indent: Dict[int, int] = {}

    # -- 内部 ---------------------------------------------------------------

    def _flush_pending(self) -> List[PowerEvent]:
        """把正在累积成员的实体事件交出去。

        ⚠️ 交出去之后锚点**必须清成 -1**。真机日志里 `Player EntityID=17` 这类声明
        与紧随的 `FULL_ENTITY - Creating` 同处一档（实测都是 dash 后 5 空格），
        锚点若留着，新实体之后那批属性行（缩进 9）会被判成"旧声明的属性"，新实体
        一个 tag 都拿不到 —— P1 的座位表直接空掉。清成 -1 后，新实体自己入
        `_pending` 时会把锚点重新设到它的位置。
        """
        if self._pending is None:
            return []
        ev, self._pending = self._pending, None
        self.stats.bump(ev.kind)
        return [ev]

    def _emit(self, ev: PowerEvent) -> List[PowerEvent]:
        self.stats.bump(ev.kind)
        return [ev]

    def _set_parent(self, cmd: str, indent: int, owner_id: Optional[int] = None) -> None:
        """记下「当前宿主命令」，并把 owner 登记到它所在的缩进深度。"""
        self._parent_cmd = cmd
        self._parent_indent = indent
        self._parent_owner_id = owner_id
        if owner_id is not None:
            self._entity_by_indent[indent] = owner_id

    def _owner_at(self, indent: int) -> Optional[int]:
        """缩进 `indent` 的裸 tag 行属于谁：取 `indent - 4` 深度上最近声明的实体。"""
        return self._entity_by_indent.get(indent - _TAG_INDENT_STEP)

    def _tag_owner(self, tag: str, value: str) -> Optional[int]:
        """属性行里能**自证宿主身份**的 tag。

        ⚠️ 只有 `ENTITY_ID` 是自证身份：它写的就是这个实体自己的 id。
        `HERO_ENTITY` **不是** —— 它写的是"这个玩家的英雄是谁"（实测
        `Player EntityID=17` 的属性里有 `tag=HERO_ENTITY value=35`），
        把它当宿主 id 会立刻产生 4 条 id 不一致（本机实测：17≠35、18≠74、23≠39、24≠86）。
        """
        if tag == "ENTITY_ID" and value.isdigit():
            return int(value)
        return None

    # -- 待选 / 已选 ---------------------------------------------------------

    @staticmethod
    def _choice_option(index: int, raw: str) -> ChoiceOption:
        """把 `[entityName=战鼓 id=3063 zone=SETASIDE …]` 解析成一个选项。

        复用 `parse_entity_ref`：这里面的字段与 Power 流里的方括号实体形态完全一样。
        """
        ref = parse_entity_ref(raw)
        return ChoiceOption(
            index=index,
            raw=raw,
            entity_id=ref.entity_id,
            name=ref.name,
            card_id=ref.card_id,
            zone=ref.zone,
            player=ref.player,
        )

    def _parse_choice_line(self, payload: str, ts: str,
                           indent: int) -> List[PowerEvent]:
        """`DebugPrintEntityChoices()` 的一行。

        一个待选块实测长这样（`Entities` 条数与 `ChoiceType` 有关）：

            id=3 Player=测试玩家#1234 TaskList=1258 ChoiceType=GENERAL CountMin=1 CountMax=1
              Source=[entityName=小型饰品 id=300 zone=PLAY zonePos=0 cardId=BG30_Trinket_1st player=2]
              Entities[1]=[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 cardId=BG32_MagicItem_416 player=2]

        事件形态刻意分成两类，让状态层不需要自己攒上下文：
          * 头行 → `CHOICE`，带 id/kind/player/source，但 `choice_option is None`
          * `Entities[i]=` 行 → 同类事件，带 `choice_id` + `choice_option`
          * 既不是头也不是选项（`Source=…`）→ 空列表（静默丢弃）
        """
        cm = _CHOICE_HEAD_RE.match(payload)
        if cm:
            cmax = cm.group("cmax")
            return self._emit(
                PowerEvent(
                    kind=EventType.CHOICE,
                    line_no=self._line_no,
                    timestamp=ts,
                    indent=indent,
                    choice_id=int(cm.group("id")),
                    choice_title=payload,
                    choice_kind=cm.group("kind"),
                    choice_player=cm.group("player"),
                    choice_count=int(cmax) if cmax else None,
                    detail=payload,
                    payload=payload,
                )
            )
        sm = _CHOICE_SOURCE_RE.match(payload)
        if sm:
            # ⚠️ `Source=` 行**不带 id**（实测形态就是裸的 `Source=[…]`），所以它不能
            # 自己成块，只能靠状态层归到「当前正在收选项的那个块」上。
            # 把原文放进 `choice_source` 交出去（`Source=GameEntity` 起手换牌也有）。
            return self._emit(
                PowerEvent(
                    kind=EventType.CHOICE,
                    line_no=self._line_no,
                    timestamp=ts,
                    indent=indent,
                    choice_source=sm.group("src"),
                    detail=payload,
                    payload=payload,
                )
            )
        em = _CHOICE_ENTITY_RE.match(payload)
        if em:
            opt = self._choice_option(int(em.group("idx")), em.group("ent"))
            return self._emit(
                PowerEvent(
                    kind=EventType.CHOICE,
                    line_no=self._line_no,
                    timestamp=ts,
                    indent=indent,
                    choice_option=opt,
                    detail=payload,
                    payload=payload,
                )
            )
        return []

    def _parse_sent_choices_line(self, payload: str, ts: str,
                                 indent: int) -> List[PowerEvent]:
        """`SendChoices()` 的一行 —— 玩家**实际**选了什么。

            id=3 ChoiceType=GENERAL
              m_chosenEntities[0]=[entityName=战鼓 id=3063 zone=SETASIDE … cardId=BG32_MagicItem_416 player=2]
        """
        hm = _SENT_HEAD_RE.match(payload)
        if hm:
            return self._emit(
                PowerEvent(
                    kind=EventType.CHOICE_RESULT,
                    line_no=self._line_no,
                    timestamp=ts,
                    indent=indent,
                    choice_id=int(hm.group("id")),
                    choice_kind=hm.group("kind"),
                    choice_title=payload,
                    detail=payload,
                    payload=payload,
                )
            )
        chm = _CHOSEN_ENTITY_RE.match(payload)
        if chm:
            opt = self._choice_option(int(chm.group("idx")), chm.group("ent"))
            return self._emit(
                PowerEvent(
                    kind=EventType.CHOICE_RESULT,
                    line_no=self._line_no,
                    timestamp=ts,
                    indent=indent,
                    choice_option=opt,
                    detail=payload,
                    payload=payload,
                )
            )
        return []

    # -- 公开接口 -----------------------------------------------------------

    def feed_line(self, line: str) -> List[PowerEvent]:
        """解析一行，返回它产生的 0..n 个事件。"""
        self._line_no += 1
        self.stats.lines_total += 1

        stripped = line.rstrip("\n").rstrip("\r")

        # 空行：实体块的结束信号。
        if not stripped.strip():
            return self._flush_pending()

        m = _LINE_RE.match(stripped)
        if not m:
            # 没有日志前缀的行（例如文件被截断、或别的格式）。跳过，不崩。
            self.stats.unknown_payloads += 1
            return self._flush_pending()

        source = m.group("source")
        raw_payload = m.group("payload")
        # 缩进是块嵌套线索，先量出来；匹配命令词时再去掉。
        indent = len(raw_payload) - len(raw_payload.lstrip())
        payload = raw_payload.strip()
        ts = m.group("ts")

        # 来源过滤：默认丢掉 PowerTaskList 那份重复输出（见模块 docstring 第 1 点）。
        if self.sources and source not in self.sources:
            return self._flush_pending()

        if source in _IGNORED_SOURCES:
            return self._flush_pending()

        self.stats.lines_power += 1

        # --- 待选 / 已选（饰品四选一、黑赐三选一、起手换牌）---
        #
        # 实测这两条流只在 `GameState` 分支下有，`PowerTaskList` 没有对应输出，
        # 所以不会像 DebugPrintPower 那样重复一份。
        #
        # ⚠️ 必须放在下面「实体属性行」判定**之前**：待选块里的
        # `Entities[0]=[…]` / `Source=…` 全是缩进行，放任下去会先去匹配
        # `_TAG_LINE_RE` 与命令词，落到 RAW 或串进别的事件里。
        #
        # ⚠️ 这里**不能**先 `_flush_pending()`：待选块紧跟在 `TAG_CHANGE` 之后时，
        # 提前 flush 会把还没吸完属性的实体截断（与「属性行不先 flush」同一个坑）。
        if source == "GameState.DebugPrintEntityChoices":
            return self._parse_choice_line(payload, ts, indent)
        if source == "GameState.SendChoices":
            return self._parse_sent_choices_line(payload, ts, indent)

        # --- 实体属性行：粘到正在累积的实体上 ---
        # 判定条件是「累积中有实体 + 这行是裸露的 tag= + 缩进正好比宿主深 4」。
        #
        # ⚠️ 这条缩进规则是探针实测出来的，不是猜的（见 tools/probe_tag_dispatch.py）：
        # 全日志 115,019 行 `tag=` 行里，实体块的属性行缩进恒为「宿主命令缩进 + 4」，
        # 其余（更深或更浅）属于外层的块级属性。早先版本只看「有没有 pending 实体」，
        # 结果把 115,019 行全吸收进实体、BLOCK_TAG 只剩 854，把 P1 还原盘面所需
        # 的信号吞掉了；只看 strip 后的 payload 更糟——`^tag=` 顶格形态会匹配到
        # 全部 115,019 行，而 `^\s+tag=` 一行都匹配不到，等于规则根本没生效。
        # 缩进必须从**未 strip** 的 raw_payload 上量。
        tm = _TAG_LINE_RE.match(raw_payload)
        tag_line = tm is not None
        # ⚠️ 属性行必须**比宿主更深**。只看「缩进正好深 4」的话，一个缩进更浅的
        # 属性行（宿主缩进 4、这行缩进 0）也会被当成属性吸收（实测踩过）。
        belongs_to_pending = (
            tm is not None
            and self._pending is not None
            and indent == self._pending_anchor_indent + _TAG_INDENT_STEP
            and indent > self._pending_anchor_indent
        )
        # ⚠️ 实体属性行**不能先 flush**：flush 会把 `_pending` 清成 None，
        # 紧随其后的属性行就没了宿主，整个实体变成空壳（槽位解析出来全是 None）。
        # 所以这里只对「非属性行」做 flush。
        if not belongs_to_pending:
            out = self._flush_pending()
        else:
            out = []
        if belongs_to_pending:
            tag = tm.group("tag")
            value = (tm.group("value") or "").strip()
            if value == "":
                # 日志里空值形如 `value=` 后跟一个空格
                value = "0"
            self._pending.tags[tag] = value
            # 属性里出现 ENTITY_ID 就等于把宿主 id 说清楚了，顺手记为归属线索。
            owner = self._tag_owner(tag, value)
            if owner is not None:
                self._pending.owner_id = owner
                self._parent_owner_id = owner
                # 自证：规则说这行属于宿主，那 ENTITY_ID 必须等于宿主 id。
                self.stats.absorbed_checked += 1
                if self._pending.entity_id is not None and owner == self._pending.entity_id:
                    self.stats.absorbed_id_match += 1
                else:
                    self.stats.absorbed_id_mismatch += 1
            return []

        # 非属性行：上面已经 flush 过累积的实体，这里只取命令词。

        cmd = _cmd_of(payload)

        # --- `CREATE_GAME` 块里的实体声明：`GameEntity EntityID=16` / `Player EntityID=17 ...` ---
        #
        # ⚠️ 必须在 `cmd is None` 的裸 tag 行分支**之前**匹配。实测这 340 行声明
        # （GameEntity 2 + Player 338）各自带 67 / 60 条缩进属性行，是
        # `PLAYER_ID` / `HERO_ENTITY` / `BACON_MAX_PLAYER_TECH_LEVEL` 的唯一来源。
        # 它们的缩进是 5（`- ` 之后 5 个空格），属性行缩进 9 —— 同样满足
        # 「属性 = 宿主 + 4」，所以识别出声明之后，后面那批属性行会正常被吸收。
        dm = _DECL_RE.match(payload)
        if dm:
            eid = int(dm.group("eid"))
            ev = PowerEvent(
                kind=EventType.PLAYER,
                line_no=self._line_no,
                timestamp=ts,
                indent=indent,
                entity_id=eid,
                detail=payload,
            )
            # 声明行上的内联 KEY=VALUE（`PlayerID=6` 等）直接落成 tag。
            # ⚠️ 键名要做 camel→SNAKE 归一，否则 `PlayerID` 会变成 `PLAYERID`，
            # 而全项目其余地方（FULL_ENTITY 属性行）用的是 `PLAYER_ID`，两套键
            # 并存会让 P1 的座位表查不到人。
            # `GameAccountId=[hi=.. lo=..]` 里的 hi/lo 也会被扫到，键名是
            # `hi`/`lo`，不与真正的 tag 冲突；`EntityID` 自己跳过。
            for fm in _DECL_FIELD_RE.finditer(payload):
                key = fm.group("key")
                if key.lower() == "entityid":
                    continue
                ev.tags[snake_tag(key)] = fm.group("val")
            self._set_parent(cmd or "", indent, eid)
            # 声明本身没有内容，但它后面一个缩进档的属性行都是它的（实测 Player
            # 声明缩进 5、属性行缩进 9，恰好满足「属性 = 宿主 + 4」，共 352 行）。
            # 挂进 `_pending` 才能被吸收分支接住；否则这些行会因为没有锚点退化成
            # BLOCK_TAG / ATTACHED_TAG，PLAYER_ID 与 HERO_ENTITY 直接丢。
            #
            # ⚠️ 只入 `_pending`，**不**立刻 `_emit`（和 FULL_ENTITY / SHOW_ENTITY
            # 同路子）：若同时 `_emit`，`_pending` 仍指向同一对象，下一次 flush 会
            # 把它再交一遍（实测 GameEntity 16 因此重复出现）。
            #
            # ⚠️ 锚点就是声明自己的缩进（真机实测 4）。声明后面紧跟的属性行是 8
            # （= 4 + 4），这样才能被吸收。声明与紧随其后的 `FULL_ENTITY - Creating`
            # 在真机里同处一档，靠 `_flush_pending` 把锚点清成 -1 来保证新实体
            # 不会被旧声明吞掉。
            self._pending = ev
            self._pending_anchor_indent = indent
            return out

        # --- 块内/游离 tag 行：`tag=CONTROLLER value=14`（无实体前缀） ---
        #
        # 实测 115,019 行。它们没有 `Entity=` 前缀，归属由缩进决定：凡是**不**满足
        # 「比上一条头命令深 4」的，就是外层块/游离实体的属性。这里如实交出
        # 缩进与宿主线索（parent_cmd / parent_indent / owner_id），
        # 真正的归属判定留给 P1 状态重建，不在解析层猜。
        if cmd is None:
            itm = _TAG_LINE_RE.match(raw_payload) or _INFO_TAG_LINE_RE.match(payload)
            if itm:
                value = (itm.group("value") or "").strip()
                if value == "":
                    value = "0"
                tag = itm.group("tag")
                # 归属 = `indent - 4` 深度上最近声明的实体（见 _entity_by_indent 注释）。
                # 退回「最近头命令的 owner」只在本行缩进 <= 4、或该深度还没声明过实体时
                # 发生；退回也没有才退化成 BLOCK_TAG。
                owner = self._owner_at(indent)
                if owner is None:
                    owner = self._parent_owner_id
                if owner is not None:
                    return out + self._emit(
                        PowerEvent(
                            kind=EventType.ATTACHED_TAG,
                            line_no=self._line_no,
                            timestamp=ts,
                            entity_id=owner,
                            indent=indent,
                            in_block=True,
                            tag=tag,
                            value=value,
                            parent_cmd=self._parent_cmd,
                            parent_indent=self._parent_indent,
                            owner_id=owner,
                        )
                    )
                self.stats.unowned_tags += 1
                return out + self._emit(
                    PowerEvent(
                        kind=EventType.BLOCK_TAG,
                        line_no=self._line_no,
                        timestamp=ts,
                        indent=indent,
                        in_block=True,
                        tag=tag,
                        value=value,
                        parent_cmd=self._parent_cmd,
                        parent_indent=self._parent_indent,
                        owner_id=self._parent_owner_id,
                    )
                )
            # 其他无命令词的行（Info[i]、SUB_SPELL_* 等）单独处理或直接丢弃，
            # 不再产生 RAW 洪水。
            im = _INFO_LINE_RE.match(payload)
            if im:
                value = im.group("value").strip()
                ref = parse_entity_ref(value) if value.startswith("[") else None
                return out + self._emit(
                    PowerEvent(
                        kind=EventType.INFO,
                        line_no=self._line_no,
                        timestamp=ts,
                        indent=indent,
                        entity=ref,
                        entity_id=ref.entity_id if ref else None,
                        card_id=ref.card_id if ref else "",
                        detail=value,
                        in_block=True,
                    )
                )
            # sub-spell / 其他已知噪声：静默丢弃，不占事件流。
            if payload.startswith(("SUB_SPELL_START", "SUB_SPELL_END")):
                return out
            self.stats.unknown_payloads += 1
            return out

        if cmd == "CREATE_GAME" or payload == "CREATE_GAME":
            self.stats.games += 1
            ev = PowerEvent(
                kind=EventType.CREATE_GAME, line_no=self._line_no, timestamp=ts, indent=indent
            )
            # ⚠️ 故意**不**调用 `_set_parent` 登记缩进锚点：`CREATE_GAME` 自身不带任何
            # 属性，紧跟在它后面、缩进 +4 的那批行是 GameEntity / Player 的**声明**
            # （实测 352 行），不是它的属性。若在这里登记锚点，`_pending` 吸收分支
            # （要求 `indent == _parent_indent + 4`，而 create 是 0、声明是 4）会把
            # 声明整行吞进 CREATE_GAME 事件，PLAYER_ID / HERO_ENTITY 全丢。
            return out + self._emit(ev)

        # `GameEntity EntityID=16` / `Player EntityID=17 PlayerID=6 ...` 已在上面
        # 的 `_DECL_RE` 分支处理（必须在裸 tag 行分支之前，此处不再重复）。

        if cmd == "FULL_ENTITY":
            fm = _FULL_ENTITY_RE.search(payload)
            if fm:
                ev = PowerEvent(
                    kind=EventType.FULL_ENTITY,
                    line_no=self._line_no,
                    timestamp=ts,
                    indent=indent,
                    entity_id=int(fm.group("id")),
                    card_id=fm.group("cardid") or "",
                )
                if fm.group("entity"):
                    ref = parse_entity_ref(fm.group("entity"))
                    if ref.entity_id is not None:
                        ev.entity_id = ref.entity_id
                self._pending = ev  # 后续 tag 行会补进来
                self._pending_anchor_indent = indent
                self._set_parent(cmd, indent, ev.entity_id)
                return out

        if cmd == "SHOW_ENTITY":
            sm = _SHOW_ENTITY_RE.search(payload)
            if sm:
                ref = parse_entity_ref(sm.group("entity"))
                ev = PowerEvent(
                    kind=EventType.SHOW_ENTITY,
                    line_no=self._line_no,
                    timestamp=ts,
                    indent=indent,
                    entity_id=ref.entity_id,
                    card_id=sm.group("cardid") or "",
                    entity=ref,
                )
                self._pending = ev
                self._pending_anchor_indent = indent
                self._set_parent(cmd, indent, ev.entity_id)
                return out

        if cmd == "CHANGE_ENTITY":
            cm = _CHANGE_ENTITY_RE.search(payload)
            if cm:
                ref = parse_entity_ref(cm.group("entity"))
                self._set_parent(cmd, indent, ref.entity_id)
                return out + self._emit(
                    PowerEvent(
                        kind=EventType.CHANGE_ENTITY,
                        line_no=self._line_no,
                        timestamp=ts,
                        indent=indent,
                        entity_id=ref.entity_id,
                        card_id=cm.group("cardid") or "",
                        entity=ref,
                    )
                )

        if cmd == "HIDE_ENTITY":
            hm = _HIDE_ENTITY_RE.search(payload)
            if hm:
                ref = parse_entity_ref(hm.group("entity"))
                return out + self._emit(
                    PowerEvent(
                        kind=EventType.HIDE_ENTITY,
                        line_no=self._line_no,
                        timestamp=ts,
                        indent=indent,
                        entity=ref,
                        entity_id=ref.entity_id,
                        tag=hm.group("tag") or "",
                        value=(hm.group("value") or "").strip(),
                    )
                )

        if cmd == "META_DATA":
            mm = _META_DATA_RE.search(payload)
            if mm:
                count = mm.group("count")
                return out + self._emit(
                    PowerEvent(
                        kind=EventType.META_DATA,
                        line_no=self._line_no,
                        timestamp=ts,
                        indent=indent,
                        meta=mm.group("meta"),
                        meta_data=mm.group("data") or "",
                        meta_count=int(count) if count is not None else None,
                    )
                )

        if cmd == "TAG_CHANGE":
            tcm = _TAG_CHANGE_RE.search(payload)
            if tcm:
                ref = parse_entity_ref(tcm.group("entity"))
                value = (tcm.group("value") or "").strip()
                if value == "":
                    value = "0"
                return out + self._emit(
                    PowerEvent(
                        kind=EventType.TAG_CHANGE,
                        line_no=self._line_no,
                        timestamp=ts,
                        indent=indent,
                        entity=ref,
                        entity_id=ref.entity_id,
                        tag=tcm.group("tag"),
                        value=value,
                    )
                )

        if cmd in ("BLOCK_START", "BLOCK_END"):
            bm = _BLOCK_RE.match(payload)
            if bm:
                ref = parse_entity_ref(bm.group("entity") or "")
                target = bm.group("target")
                sub = bm.group("sub")
                block = BlockRef(
                    kind=bm.group("kind"),
                    block_type=bm.group("btype") or "",
                    entity=ref.raw,
                    entity_id=ref.entity_id,
                    effect_card_id=(bm.group("effect") or "").strip(),
                    trigger_keyword=(bm.group("trigger") or "").strip(),
                    target=int(target) if target is not None else None,
                    sub_option=int(sub) if sub is not None else None,
                )
                kind = (
                    EventType.BLOCK_START
                    if bm.group("kind") == "BLOCK_START"
                    else EventType.BLOCK_END
                )
                # ⚠️ BLOCK_START **不**登记深度归属：它的 `Entity=` 是「谁触发了这个块」，
                # 不是「块内属性属于谁」。例如
                #   BLOCK_START BlockType=TRIGGER Entity=[... id=75 ... cardId=TB_BaconShop_8P_PlayerE ...]
                # 之后是块内实体的属性行（真实日志里紧随 `FULL_ENTITY - Creating ID=244`）。
                # 早先把它当成 owner 登记，实测 766 条 ATTACHED_TAG 归属全错。
                # 只更新「最近头命令」线索（parent_cmd/parent_indent），深度表保持不动。
                if kind == EventType.BLOCK_START:
                    self._set_parent(cmd, indent, None)
                return out + self._emit(
                    PowerEvent(
                        kind=kind,
                        line_no=self._line_no,
                        timestamp=ts,
                        indent=indent,
                        block=block,
                        entity_id=ref.entity_id,
                    )
                )

        # 其他：保留原文，便于后续补解析（如 DebugPrintGame 的 GameType 等）。
        return out + self._emit(
            PowerEvent(
                kind=EventType.RAW,
                line_no=self._line_no,
                timestamp=ts,
                indent=indent,
                payload=payload,
            )
        )

    def feed(self, text: str) -> List[PowerEvent]:
        """喂一段多行文本。"""
        events: List[PowerEvent] = []
        for line in text.splitlines():
            events.extend(self.feed_line(line))
        return events

    def flush(self) -> List[PowerEvent]:
        """流结束时调用，交出最后一个未闭合的实体事件。"""
        return self._flush_pending()


# ---------------------------------------------------------------------------
# 文件级便捷函数
# ---------------------------------------------------------------------------


def parse_file(
    path: str | Path,
    sources: Tuple[str, ...] = DEFAULT_SOURCES,
    limit_lines: Optional[int] = None,
) -> Tuple[List[PowerEvent], ParseStats]:
    """整文件解析（历史日志回归测试用）。

    ⚠️ 真实 `Power.log` 有 90-100 MB，这个函数会把事件全放进内存。
    在线场景请用 `tailer.LogTailer` 流式处理。
    """
    parser = PowerLogParser(sources=sources)
    events: List[PowerEvent] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if limit_lines is not None and i >= limit_lines:
                break
            events.extend(parser.feed_line(line))
    events.extend(parser.flush())
    return events, parser.stats


def iter_file(
    path: str | Path,
    sources: Tuple[str, ...] = DEFAULT_SOURCES,
) -> Iterator[PowerEvent]:
    """流式解析整文件，事件逐个产出（内存友好）。"""
    parser = PowerLogParser(sources=sources)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for ev in parser.feed_line(line):
                yield ev
    for ev in parser.flush():
        yield ev


__all__ = [
    "PowerLogParser",
    "parse_entity_ref",
    "parse_file",
    "iter_file",
    "DEFAULT_SOURCES",
    "parse_source_ref",
]
