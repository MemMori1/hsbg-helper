"""Power.log 事件模型。

设计原则：**只做无状态解析**，不在这里维护实体表。实体表（EntityID → 属性）
属于 P1 的状态重建层（`state.py`），事件层只负责把一行文本变成结构化对象。

这样拆的收益：解析器可以对着历史日志做纯函数式回归测试；
而状态重建的错误不会被误判成解析错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


class EventType:
    """事件类型常量。用普通类而不是 Enum，打印/比较更省事。"""

    CREATE_GAME = "CREATE_GAME"
    FULL_ENTITY = "FULL_ENTITY"      # 实体首次完整出现
    SHOW_ENTITY = "SHOW_ENTITY"      # 实体揭示（补一版完整快照）
    CHANGE_ENTITY = "CHANGE_ENTITY"  # 实体替换（变形/三连合并）
    HIDE_ENTITY = "HIDE_ENTITY"      # 实体隐藏（实测 1,535 次，含中文名快照）
    META_DATA = "META_DATA"          # 元数据（发现/目标选择等，实测 1,080 次）
    INFO = "INFO"                    # META_DATA 的 Info[i] 明细（可能直接带中文名+CardID）
    TAG_CHANGE = "TAG_CHANGE"        # 单个属性变化，最主要的增量来源
    BLOCK_TAG = "BLOCK_TAG"          # ⚠️ 块内部缩进的 tag 行（无实体前缀，见下）
    ATTACHED_TAG = "ATTACHED_TAG"    # 缩进不满足宿主+4，但宿主 id 已知 → 归属确定
    BLOCK_START = "BLOCK_START"      # 动作块开始（可嵌套）
    BLOCK_END = "BLOCK_END"
    PLAYER = "PLAYER"                # 顶层 PLAYER 行（PLAYER_ID / HERO_ENTITY）
    CHOICE = "CHOICE"                # 一次待选（饰品四选一 / 黑赐三选一 / 起手换牌）
    CHOICE_RESULT = "CHOICE_RESULT"  # 玩家实际选了哪个（SendChoices）
    TURN = "TURN"                    # TURN 标记行
    RAW = "RAW"                      # 未识别但保留的行（便于排障）


#: 属性名 → 是否是我们关心的核心属性。仅用于 CLI 过滤，不参与解析正确性。
_CORE_TAGS = frozenset(
    {
        "ZONE",
        "ZONE_POSITION",
        "CONTROLLER",
        "ATK",
        "HEALTH",
        "DAMAGE",
        "CARDTYPE",
        "TECH_LEVEL",
        "RESOURCES",
        "RESOURCES_USED",
        "PLAYER_ID",
        "HERO_ENTITY",
        "CURRENT_PLAYER",
        "TURN",
        "STEP",
        "PLAYSTATE",
        "EXHAUSTED",
    }
)


@dataclass
class BlockRef:
    """一个被解析出来的动作块标记（BLOCK_START / BLOCK_END）。"""

    kind: str  # EventType.BLOCK_START / BLOCK_END
    block_type: str = ""  # TRIGGER / POWER / PLAY / ATTACK / DEATHS / MOVE_MINION ...
    entity: str = ""
    entity_id: Optional[int] = None
    effect_card_id: str = ""
    trigger_keyword: str = ""
    target: Optional[int] = None
    sub_option: Optional[int] = None


@dataclass
class EntityRef:
    """日志里对实体的三种引用形态归一化后的结果。

    实测出现的三种形态：
      1. 纯数字 ID           -> Entity=17
      2. 方括号详细形态      -> Entity=[entityName=UNKNOWN ENTITY [cardType=INVALID]
                                          id=244 zone=SETASIDE zonePos=0 cardId= player=6]
      3. 中文实体名          -> Entity=调酒师鲍勃 / Entity=测试玩家#1234

    方括号形态顺带携带 zone/cardId/player 快照，是修复实体表的免费信息源。
    """

    raw: str
    entity_id: Optional[int] = None
    name: str = ""
    card_id: str = ""
    zone: str = ""
    zone_pos: Optional[int] = None
    player: Optional[int] = None
    card_type: str = ""

    @property
    def is_id_only(self) -> bool:
        return self.raw.isdigit()

    @property
    def is_known(self) -> bool:
        """日志是否给出了可用的名字（排除 UNKNOWN ENTITY / N0NAME 噪声）。"""
        if not self.name:
            return False
        # ⚠️ 实测形态是 `UNKNOWN ENTITY [cardType=INVALID]`，带后缀，
        # 不能用 == 比，否则 22,361 行噪声会被当成有效名字。
        return not (self.name.startswith("UNKNOWN ENTITY") or self.name == "N0NAME")


@dataclass
class ChoiceOption:
    """待选列表里的一个选项。

    实测形态（起手换牌 / 黑赐三选一 / 饰品四选一都用同一套结构）：

        Entities[1]=[entityName=战鼓 id=3063 zone=SETASIDE zonePos=0 \
                     cardId=BG32_MagicItem_416 player=2]

    起手换牌时 `zone` 是 `HAND`，其余是 `SETASIDE`。
    """

    index: int
    raw: str = ""
    entity_id: Optional[int] = None
    name: str = ""
    card_id: str = ""
    zone: str = ""
    player: Optional[int] = None

    @property
    def is_known(self) -> bool:
        if not self.name:
            return False
        return not (self.name.startswith("UNKNOWN ENTITY") or self.name == "N0NAME")

    @property
    def display_name(self) -> str:
        return self.name if self.is_known else (self.card_id or "?")


@dataclass
class PowerEvent:
    """一行日志解析出的一个事件。"""

    kind: str
    line_no: int
    timestamp: str = ""

    # CREATE_GAME
    entity_id: Optional[int] = None
    card_id: str = ""

    # FULL_ENTITY / SHOW_ENTITY / CHANGE_ENTITY
    tags: dict = field(default_factory=dict)

    # TAG_CHANGE
    entity: Optional[EntityRef] = None
    tag: str = ""
    value: str = ""

    # BLOCK_*
    block: Optional[BlockRef] = None

    # META_DATA
    meta: str = ""
    meta_data: str = ""
    meta_count: Optional[int] = None

    # META_DATA 的 Info[i] 明细：`Info[0] = [entityName=欢悦 id=312 ... cardId=BG36_110 ...]`
    info_index: Optional[int] = None

    # 缩进（块嵌套深度线索）
    indent: int = 0

    # 属性行的归属线索。实测规律：实体/块头命令的缩进是 N，它的属性行缩进恒为 N+4。
    # 所以 tag 行能凭「比上一条命令深 4」确定性地绑定到宿主，不需要猜。
    parent_cmd: str = ""
    parent_indent: int = -1
    owner_id: Optional[int] = None

    # 是否位于动作块内部（块内 tag 行没有实体前缀，需要靠块上下文归属）
    in_block: bool = False

    # CHOICE / CHOICE_RESULT：待选与已选
    #
    # 实测来源 `GameState.DebugPrintEntityChoices()`（选项列表，每个选项一行）
    # 与 `GameState.SendChoices()`（玩家实际选了什么）。两者靠 `choice_id` 配对：
    # `CHOICE` 只在 `id=` 那一行发一次（带 title/kind/source），后续 `Entities[i]=`
    # 行各自发一条 ack（`choice_option` 有值、`choice_id` 相同）。
    choice_id: Optional[int] = None
    choice_title: str = ""       # `id=3 Player=… TaskList=1258 ChoiceType=GENERAL …`
    choice_kind: str = ""        # MULLIGAN / GENERAL / ...
    choice_source: str = ""      # `Source=…` 原文（GameEntity 或方括号实体）
    choice_player: str = ""      # `Player=` 后面的账号名（用来判本地玩家）
    choice_option: Optional[ChoiceOption] = None
    choice_count: Optional[int] = None  # CountMin/CountMax 里的 Max

    # TURN 等标记行的附带信息
    detail: str = ""

    # RAW 行原文
    payload: str = ""

    @property
    def is_bacon_tag(self) -> bool:
        return self.tag.startswith("BACON_")

    @property
    def is_core_tag(self) -> bool:
        return self.tag in _CORE_TAGS


@dataclass
class ParseStats:
    """解析统计，用来判断"是不是漏了东西"。"""

    lines_total: int = 0
    lines_power: int = 0            # 属于 Power 流的行（排除 LoadingScreen 等）
    events: int = 0
    games: int = 0
    unknown_payloads: int = 0
    by_kind: dict = field(default_factory=dict)

    # 归属规则的自证计数（见 tools/verify_attr_rule.py）。
    # 被吸收的属性行里带 `tag=ENTITY_ID` 的，其值应当等于宿主 id；
    # 这是「缩进 = 宿主+4」这条规则正确性的硬判据，而不是数数。
    absorbed_checked: int = 0        # 带 ENTITY_ID 的被吸收行数
    absorbed_id_match: int = 0       # 其中与宿主 id 一致的
    absorbed_id_mismatch: int = 0    # 其中不一致的（>0 就是规则错了）
    unowned_tags: int = 0            # 连深度表都定不出归属的裸 tag 行（应接近 0）

    def bump(self, kind: str) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        self.events += 1

    def as_dict(self) -> dict:
        return {
            "lines_total": self.lines_total,
            "lines_power": self.lines_power,
            "events": self.events,
            "games": self.games,
            "unknown_payloads": self.unknown_payloads,
            "by_kind": dict(sorted(self.by_kind.items(), key=lambda kv: -kv[1])),
        }


def iter_kinds(events: Iterable[PowerEvent]) -> Iterable[str]:
    for ev in events:
        yield ev.kind


__all__ = [
    "EventType",
    "BlockRef",
    "EntityRef",
    "ChoiceOption",
    "PowerEvent",
    "ParseStats",
    "iter_kinds",
]
