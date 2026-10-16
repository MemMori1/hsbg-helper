#!/usr/bin/env python3
"""P1（第一步）：从事件流增量重建酒馆战棋盘面。

设计要点（全部由 P0/P1 实测证据支撑，见 docs/需求分析.md §11）：

1. **实体表是唯一真相源**。`FULL_ENTITY` / `SHOW_ENTITY` / `PLAYER` 建立实体，
   `TAG_CHANGE` / `ATTACHED_TAG` 更新属性，`HIDE_ENTITY` 标记离场。
2. **商店认法：`TB_BaconShop_DragBuy` 的 `tag=2442`**。这个 tag 指向"槽位里
   真正可买的那张随从实体"，归零表示该槽被买走/刷新。实测：
   回合 1 在 `MAIN_START` 建立 4 个槽，买走即刻归零，回合 2 `MAIN_READY` 全部归零。
   —— 比"按 CONTROLLER 猜"可靠得多（CONTROLLER 在同一局内会变）。
3. **座位：英雄实体上的 `PLAYER_ID`**。实测战棋英雄（如 `TB_BaconShop_HERO_91`）
   带 `PLAYER_ID=6`，取值 1..8 恰好 8 个，本地玩家是其中带真实 `GameAccountId`
   的那个（`hi != 0`）。
4. **回合/阶段**：`GameEntity tag=TURN` 给回合数，`tag=STEP` 给阶段，
   `tag=CURRENT_PLAYER value=1` 给当前行动方。⚠️ tag 值恒为**字符串**。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .parser import parse_source_ref as _parse_source_ref


def _source_ref(raw: str):
    """解析 `Source=[…]` 原文 —— 解析失败不能让整个重建崩掉。"""
    try:
        return _parse_source_ref(raw)
    except Exception:  # pragma: no cover - 防御性
        from .events import EntityRef
        return EntityRef(raw=raw)

# 商店槽位的引用 tag（`TB_BaconShop_DragBuy` 上）
TAG_SHOP_SLOT_REF = "2442"
CARD_DRAG_BUY = "TB_BaconShop_DragBuy"
#: 酒馆**法术**槽位。和随从槽位是两套实体，但用同一个 `tag=2442` 指向"这一格里
#: 真正能买的那张牌"。漏掉它整个商店就少一块 —— 实测会刷出 `酒馆币` /
#: `主厨甄选` / `联盟旗帜` 这类 1~4 金的法术。
CARD_DRAG_BUY_SPELL = "TB_BaconShop_DragBuy_Spell"
CARD_BOB = "TB_BaconShopBob"
CARD_LOCAL_PLAYER_ENCH = "TB_BaconShop_8P_PlayerE"

# 阶段名
STEP_RECRUIT = ("MAIN_READY", "MAIN_START_TRIGGERS", "MAIN_START", "MAIN_ACTION")
STEP_COMBAT = ("MAIN_COMBAT", "COMBAT", "MAIN_END", "MAIN_CLEANUP", "MAIN_NEXT")


def _int(v) -> Optional[int]:
    """日志里的值恒为字符串，统一转 int；转不了返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return None


@dataclass
class Entity:
    """日志里的一条实体。属性用日志 tag 原名存，避免翻译层出错。"""
    entity_id: int
    card_id: str = ""
    name: str = ""
    tags: Dict[str, str] = field(default_factory=dict)

    # ---- 常用属性的只读视图 ----
    @property
    def zone(self) -> str:
        return self.tags.get("ZONE", "")

    @property
    def zone_pos(self) -> Optional[int]:
        return _int(self.tags.get("ZONE_POSITION"))

    @property
    def controller(self) -> Optional[int]:
        return _int(self.tags.get("CONTROLLER"))

    @property
    def player_id(self) -> Optional[int]:
        return _int(self.tags.get("PLAYER_ID"))

    @property
    def card_type(self) -> str:
        return self.tags.get("CARDTYPE", "")

    @property
    def tech_level(self) -> Optional[int]:
        """酒馆等级。

        ⚠️ 玩家的酒馆等级写在 `PLAYER_TECH_LEVEL` 上（实测本地英雄 104：
        `TAG_CHANGE ... id=104 ... tag=PLAYER_TECH_LEVEL value=2`），
        `TECH_LEVEL` 是**随从的星级**和升本按钮的目标档位，两者含义不同。
        英雄实体上通常只有 `PLAYER_TECH_LEVEL`；随从只有 `TECH_LEVEL`。
        """
        v = self.tags.get("PLAYER_TECH_LEVEL")
        if v is not None and v != "":
            return _int(v)
        return _int(self.tags.get("TECH_LEVEL"))

    @property
    def attack(self) -> Optional[int]:
        return _int(self.tags.get("ATK"))

    @property
    def health(self) -> Optional[int]:
        return _int(self.tags.get("HEALTH"))

    @property
    def damage(self) -> Optional[int]:
        return _int(self.tags.get("DAMAGE"))

    @property
    def golden(self) -> bool:
        return str(self.tags.get("PREMIUM", "0")) not in ("0", "")

    @property
    def in_play(self) -> bool:
        return self.zone == "PLAY"


@dataclass
class ShopSlot:
    """商店里的一个槽位（随从或法术）。"""
    slot_entity_id: int          # DragBuy 实体 id（槽位本身）
    minion_entity_id: Optional[int]  # 指向的随从实体；None = 已买走/空
    card_id: str = ""
    name: str = ""
    tech_level: Optional[int] = None
    attack: Optional[int] = None
    health: Optional[int] = None
    golden: bool = False
    controller: Optional[int] = None
    #: `"minion"` 或 `"spell"` —— 法术走 `TB_BaconShop_DragBuy_Spell`，
    #: 没有攻/血，费用也不一定是 3。
    kind: str = "minion"
    #: 这张牌的购买费用。随从固定 `buy_cost`（3），法术按卡表 `cost`（0~4 实测）。
    cost: Optional[int] = None
    # ⚠️ 默认必须是 False：`local_ctrl` 未知时不能默认算本地（战斗中会给对手生成
    # 同类 DragBuy 槽位），默认 True 会把对手的商店混进"可买"列表。
    belongs_to_local: bool = False

    @property
    def empty(self) -> bool:
        return self.minion_entity_id is None

    @property
    def is_spell(self) -> bool:
        return self.kind == "spell"


@dataclass
class ChoiceOption:
    """一个待选项（饰品四选一 / 黑赐三选一 / 起手换牌都用它）。"""
    index: int
    entity_id: Optional[int] = None
    name: str = ""
    card_id: str = ""
    zone: str = ""
    # 黑暗之赐：赐福是**另一个实体**，靠选项实体上的 `DARK_GIFT_ENTITY` 标签指过来。
    # 实测（`Hearthstone_2026_10_14_21_48_48/Power.log`）选项是随从、赐福是
    # `BG36_MidGameEffect_000tNN` 卡，"带不带特殊词条"完全由赐福决定 ——
    # 光看随从本身是挑不出来的（同一只随从可以配不同赐福）。
    gift_entity_id: Optional[int] = None
    gift_card_id: str = ""
    gift_name: str = ""

    @property
    def known(self) -> bool:
        if not self.name:
            return False
        return not (self.name.startswith("UNKNOWN ENTITY") or self.name == "N0NAME")

    @property
    def has_gift(self) -> bool:
        return bool(self.gift_card_id or self.gift_name)


@dataclass
class PendingChoice:
    """一次等待玩家做决定的选一。

    ⚠️ 起手换牌（`ChoiceType=MULLIGAN`）也算，但规则引擎不管它 ——
    它靠 `kind` 区分，`options` 结构完全一样。
    """
    choice_id: Optional[int] = None
    kind: str = ""                 # MULLIGAN / GENERAL
    source_card_id: str = ""       # 小型饰品 / 大型饰品 / 黑赐总开关
    source_name: str = ""
    player: str = ""               # 日志里的账号名
    is_local: bool = False
    options: List[ChoiceOption] = field(default_factory=list)
    expected: Optional[int] = None  # CountMax，用来判断"选项收全了没有"
    selected: Optional[ChoiceOption] = None  # `SendChoices` 给出的答案

    @property
    def complete(self) -> bool:
        if not self.options:
            return False
        if self.expected is None:
            return True
        return len(self.options) >= self.expected

    @property
    def is_trinket(self) -> bool:
        return self.source_card_id.startswith("BG30_Trinket_")

    @property
    def is_dark_gift(self) -> bool:
        return self.source_card_id == "BG36_MidGameEffect_010"

    @property
    def trinket_size(self) -> str:
        """`small` / `large` —— 卡表里是 `BG30_Trinket_1st` / `_2nd`。"""
        if self.source_card_id.endswith("_1st"):
            return "small"
        if self.source_card_id.endswith("_2nd"):
            return "large"
        return ""


@dataclass
class Trinket:
    """已装备的饰品（小/大各一个）。"""
    size: str = ""          # small / large
    card_id: str = ""
    name: str = ""
    entity_id: Optional[int] = None


@dataclass
class DarkGiftRecord:
    """一次黑暗之赐落到了谁身上。

    ⚠️ 日志**不直接说**"选了哪个赐福"：三选一给的选项是**随从**
    （`Entities[i]=[entityName=海藻护卫 … cardId=BG36_701]`），玩家是在挑"让哪只
    随从吃赐福"。所以这里记的是随从，赐福本身靠随从实体上的 `DARK_GIFT_ENTITY`
    在规则层补齐（`DARK_GIFT_ENTITY` 每局末态会被清零，见 `docs/实现笔记.md`）。
    """
    minion_name: str = ""
    minion_card_id: str = ""
    minion_entity_id: Optional[int] = None
    choice_id: Optional[int] = None


@dataclass
class ChoiceRecord:
    """历史里的一次选一（给面板/复盘看，不参与规则打分）。"""
    choice_id: Optional[int] = None
    kind: str = ""
    source_name: str = ""
    selected_name: str = ""
    selected_card_id: str = ""


@dataclass
class Seat:
    """一个玩家的座位信息。"""
    player_id: int
    hero_entity_id: Optional[int] = None
    hero_card_id: str = ""
    hero_name: str = ""
    is_local: bool = False
    health: Optional[int] = None
    max_health: Optional[int] = None
    damage: Optional[int] = None
    tech_level: Optional[int] = None
    max_tech_level: Optional[int] = None
    resources: Optional[int] = None
    resources_used: Optional[int] = None
    died_last_combat: bool = False
    won_last_combat: bool = False

    @property
    def gold(self) -> Optional[int]:
        """剩余金币 = RESOURCES - RESOURCES_USED（两者都是日志原始 tag）。"""
        if self.resources is None:
            return None
        return self.resources - (self.resources_used or 0)

    @property
    def effective_health(self) -> Optional[int]:
        """有效血量 = 英雄的 `tags[HEALTH]`（上限）− `DAMAGE`。

        ⚠️ 战棋里英雄的 `HEALTH` tag 是**上限**不是当前值，当前值要靠 `DAMAGE` 反推
        （实测本地英雄 104 有 `DAMAGE=32`、`HEALTH` 是 40，即 8 血）。
        """
        if self.max_health is None:
            return self.health
        # ⚠️ 出局后 DAMAGE 会超过上限（实测末局对手显示 -12 血），下界归零更好读。
        return max(0, self.max_health - (self.damage or 0))


@dataclass
class GameState:
    """某一时刻的完整盘面。"""
    turn: int = 0
    step: str = ""
    current_player_id: Optional[int] = None
    local_player_id: Optional[int] = None
    seats: Dict[int, Seat] = field(default_factory=dict)
    shop: List[ShopSlot] = field(default_factory=list)
    #: 酒馆法术槽位（`TB_BaconShop_DragBuy_Spell`）。实测一局里 0~1 张常驻，
    #: 和随从槽位分开存主要是因为它们**没有攻/血、费用也不是 3**。
    spells: List[ShopSlot] = field(default_factory=list)
    board: List[Entity] = field(default_factory=list)
    hand: List[Entity] = field(default_factory=list)
    entities: Dict[int, Entity] = field(default_factory=dict)

    #: 当前等待玩家做的选一（选项收全后才有值；被选了或被新选择顶掉就清空）
    pending_choice: Optional[PendingChoice] = None
    #: 已经选定的饰品（小/大）
    trinkets: List[Trinket] = field(default_factory=list)
    #: 最近一次"选了哪个"（调试与面板都用得上）
    last_choice: Optional[ChoiceOption] = None
    #: 已选的黑暗之赐（按选中顺序）
    dark_gifts: List[DarkGiftRecord] = field(default_factory=list)
    #: 本局经历过的所有选一（起手换牌 / 黑赐 / 饰品）
    choice_log: List[ChoiceRecord] = field(default_factory=list)

    @property
    def trinket(self) -> Optional[Trinket]:
        """小饰品（没有则 None）。"""
        for t in self.trinkets:
            if t.size == "small":
                return t
        return None

    @property
    def large_trinket(self) -> Optional[Trinket]:
        """大饰品（没有则 None）。"""
        for t in self.trinkets:
            if t.size == "large":
                return t
        return None

    # ---- 派生视图 ----
    @property
    def local_seat(self) -> Optional[Seat]:
        if self.local_player_id is None:
            return None
        return self.seats.get(self.local_player_id)

    @property
    def in_recruit_phase(self) -> bool:
        return self.step in STEP_RECRUIT

    @property
    def shop_minions(self) -> List[ShopSlot]:
        return [s for s in self.shop if not s.empty and s.belongs_to_local]

    @property
    def shop_spells(self) -> List[ShopSlot]:
        """本地玩家当前可买的酒馆法术（在售、非空）。"""
        return [s for s in self.spells if not s.empty and s.belongs_to_local]

    @property
    def opponents(self) -> List[Seat]:
        return [s for s in self.seats.values()
                if not s.is_local and s.hero_card_id]

    def buy_candidates(self) -> List[ShopSlot]:
        """当前金币买得起的商店随从。"""
        seat = self.local_seat
        if seat is None or seat.gold is None:
            return self.shop_minions
        return self.shop_minions


class StateBuilder:
    """把 `PowerEvent` 流增量喂成一个 `GameState`。

    用法::

        b = StateBuilder()
        for ev in iter_file(path):
            b.apply(ev)
        state = b.state
    """

    def __init__(self):
        self._state = GameState()
        self._hero_by_player: Dict[int, int] = {}   # PLAYER_ID -> hero entity id
        self._ench_owner: Dict[int, int] = {}       # 附魔 id -> 玩家实体 id
        self._local_account_player: Optional[int] = None
        self._local_player_name: str = ""
        self._named_hero: Dict[str, int] = {}       # 具名玩家 -> 英雄实体 id
        self._local_account_player_hero: Optional[int] = None  # 账号实体对应的英雄
        #: 逐帧重算的视图。⚠️ 性能关键：`_refresh_*` 全程 O(所有实体)，若每个事件
        #: 都调一次，96 MB 日志要 240 s（实测 2.1 亿次函数调用）。改成惰性——
        #: 有事件改动实体/座位时置脏，读 `.state` 时才重算。
        self._dirty_shop = True
        self._dirty_boards = True
        self._dirty_seats = True
        self._shop_slots: List[ShopSlot] = []

        #: 正在累积选项的那个待选块。⚠️ 选项行（`Entities[i]=`）**不带 id**，
        #: 只能靠「最近一个头行」认领，所以这里必须有缓冲。
        self._choosing: Optional[PendingChoice] = None

    @property
    def state(self) -> GameState:
        """当前盘面快照（惰性重算受影响的视图）。"""
        if self._dirty_shop or self._dirty_boards or self._dirty_seats:
            self._refresh()
        return self._state

    def _refresh(self) -> None:
        # ⚠️ 顺序有意义：`_refresh_seats` 必须先跑，因为 `_refresh_boards` 要靠
        # 「本地座位的 hero_entity_id → 英雄实体的 CONTROLLER」来排除对手随从；
        # 座位还没填时 local_ctrl 为 None，过滤会整体失效（实测会把商店随从算进场面）。
        # 视图之间还有别的依赖，所以在**改动了东西**时再跑一轮（上限 4 轮防死循环）。
        for _ in range(4):
            changed = False
            shop_before = len(self._state.shop)
            if self._dirty_seats or changed:
                self._dirty_seats = False
                # ⚠️ 座位数变化时 `_local_controller()`（依赖座位→英雄实体的
                # CONTROLLER）这一轮才刚可用，商店槽位归属会算错且**不会有下一轮**
                # 机会修正（收敛循环见 changed 为 False 就 break）→ 必须上报 changed。
                if self._refresh_seats():
                    changed = True
            # ⚠️ `_refresh_boards` 必须在 `_refresh_shop` **之前**：商店槽位是否
            # 属于本地，靠的是本地英雄的 CONTROLLER，而那个值由 boards 解析出来。
            # 顺序反了会让 `local_ctrl` 恒为 None，`belongs_to_local` 全 True（实测翻车）。
            if self._dirty_boards or changed:
                self._dirty_boards = False
                if self._refresh_boards():
                    changed = True
            if self._dirty_shop or changed:
                self._dirty_shop = False
                # ⚠️ 返回 True 只代表"槽位集合变了"。归属重算不改变集合，所以座位/
                # 本地玩家刚解析出来时，即使 `changed` 已是 False 也必须再算一次——
                # 早先只在返回 True 时上报 changed，会让 `local_ctrl` 从 None 变成 6
                # 的**那一轮**归属没被修正，收敛循环直接 break 后永久停在错误值
                # （实测：本地槽位 `belongs_to_local=False`）。每轮无条件重跑最省事。
                self._refresh_shop()
                if len(self._state.shop) != shop_before:
                    changed = True
            if not changed:
                break

    # ------------------------------------------------------------------
    def new_game(self) -> None:
        """开始新的一局：清空所有累积状态，保留 builder 自身可复用。"""
        self._state = GameState()
        self._hero_by_player.clear()
        self._ench_owner.clear()
        self._local_account_player = None
        self._local_player_name = ""
        self._named_hero.clear()
        self._local_account_player_hero = None
        self._choosing = None
        self._dirty_shop = self._dirty_boards = self._dirty_seats = True

    # ------------------------------------------------------------------
    def apply(self, ev) -> None:
        """喂一个事件。返回 None；状态在 self._state 上累积。"""
        kind = getattr(ev, "kind", None)

        # 一局结束、下一局开始时（新号 `CREATE_GAME`）必须清场，否则上一局的
        # 座位/英雄/实体全部带进下一局（实测第二局的 P8 会覆盖第一局的本地座位）。
        if kind == "CREATE_GAME" and self._state.entities:
            self.new_game()
        self._dirty_seats = True

        if kind == "PLAYER":
            self._on_player(ev)
        elif kind in ("FULL_ENTITY", "SHOW_ENTITY", "CHANGE_ENTITY"):
            self._on_entity(ev)
        elif kind in ("TAG_CHANGE", "ATTACHED_TAG", "BLOCK_TAG"):
            self._refresh_named_hero(ev)
            self._on_tag_change(ev)
        elif kind == "HIDE_ENTITY":
            self._on_hide(ev)
        elif kind == "CHOICE":
            self._on_choice(ev)
        elif kind == "CHOICE_RESULT":
            self._on_choice_result(ev)

        # 回合 / 阶段 / 行动方
        if getattr(ev, "tag", None) == "TURN" and self._is_game_entity(ev):
            t = _int(ev.value)
            if t is not None:
                self._state.turn = t
                self._dirty_seats = True
        elif getattr(ev, "tag", None) == "STEP":
            self._state.step = str(ev.value or "")
        elif getattr(ev, "tag", None) == "CURRENT_PLAYER" and str(ev.value) == "1":
            pid = self._player_id_of(ev)
            if pid is not None:
                self._state.current_player_id = pid
                self._dirty_seats = True

        # 视图按需重算（见 `state` 属性）；这里只标脏，不再逐事件全量刷新。
        self._dirty_shop = True
        self._dirty_boards = True

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _on_player(self, ev) -> None:
        """`GameEntity EntityID=N` / `Player EntityID=N PlayerID=M ...`"""
        eid = ev.entity_id
        if eid is None:
            return
        e = self._entity(eid)
        for k, v in (ev.tags or {}).items():
            e.tags[k] = str(v)
        detail = getattr(ev, "detail", "") or ""
        if "GameAccountId" in detail:
            # 本地玩家的账号 hi 非 0；电脑 / 鲍勃是 hi=0 lo=0
            compact = detail.replace(" ", "")
            addr = e.tags.get("GAME_ACCOUNT_ID", "")
            if "hi=0lo=0" in compact or "hi=0lo=0" in addr:
                # 电脑 / 鲍勃：账号 hi=0 lo=0，不是本地玩家
                self._player_entity = None
            else:
                self._local_account_player = eid
                self._player_entity = eid
        self._index_entity(e)

    def _on_entity(self, ev) -> None:
        eid = ev.entity_id
        if eid is None:
            return
        e = self._entity(eid)
        if ev.card_id:
            e.card_id = ev.card_id
        if ev.entity is not None:
            if getattr(ev.entity, "name", ""):
                e.name = ev.entity.name
            if getattr(ev.entity, "zone", ""):
                e.tags["ZONE"] = ev.entity.zone
            zp = getattr(ev.entity, "zone_pos", None)
            if zp:
                e.tags["ZONE_POSITION"] = str(zp)
            pl = getattr(ev.entity, "player", None)
            if pl:
                e.tags["CONTROLLER"] = str(pl)
        for k, v in (ev.tags or {}).items():
            e.tags[k] = str(v)
        self._index_entity(e)

    def _on_tag_change(self, ev) -> None:
        eid = ev.entity_id
        tag = getattr(ev, "tag", None)
        if not tag:
            return
        if eid is None:
            # ⚠️ 大量事件是「具名实体」形态（`Entity=测试玩家#1234`），没有数字
            # id。金币就藏在这里：`RESOURCES` / `RESOURCES_USED` 一局各十几条，
            # 全是具名形态。早先这里直接 return，P1 的金币永远是 None。
            self._on_named_tag_change(ev)
            return
        e = self._entity(eid)
        e.tags[tag] = str(ev.value)
        if not e.card_id and getattr(ev, "card_id", ""):
            e.card_id = ev.card_id
        self._index_entity(e)

    def _on_named_tag_change(self, ev) -> None:
        """`TAG_CHANGE Entity=<玩家名> tag=...`（无数字 id）。"""
        name = ""
        ent = getattr(ev, "entity", None)
        if ent is not None:
            name = getattr(ent, "name", "") or ""
        if not name or name in ("GameEntity", "N0NAME"):
            return
        tag = str(ev.tag).upper()

        # 本地玩家名字的认定：`CURRENT_PLAYER value=1` 一定指本地玩家，
        # 此时顺着该玩家的 `HERO_ENTITY` 找到英雄实体、再取其 `PLAYER_ID`，
        # 与 `_resolve_local_player()` 的结果比对确认，命中即锁定名字。
        if tag == "CURRENT_PLAYER" and str(ev.value) == "1":
            local_pid = self._resolve_local_player()
            if local_pid is not None:
                hid = self._hero_entity_of_named(ev)
                if hid is not None and self._hero_by_player.get(local_pid) == hid:
                    self._local_player_name = name
            return

        if not self._local_player_name or name != self._local_player_name:
            return

        pid = self._resolve_local_player()
        if pid is None:
            return
        seat = self._ensure_seat(pid)
        if tag == "RESOURCES":
            v = _int(ev.value)
            if v is not None:
                seat.resources = v
        elif tag == "RESOURCES_USED":
            seat.resources_used = _int(ev.value)

    def _hero_entity_of_named(self, ev) -> Optional[int]:
        """具名玩家实体当前指向的英雄实体 id（靠随行 tag 行缓存的 `HERO_ENTITY`）。"""
        name = getattr(getattr(ev, "entity", None), "name", "") or ""
        if not name:
            return None
        hid = self._named_hero.get(name)
        return hid

    def _refresh_named_hero(self, ev) -> None:
        """记录 `TAG_CHANGE Entity=<玩家名> tag=HERO_ENTITY value=N`。"""
        if str(getattr(ev, "tag", "")).upper() != "HERO_ENTITY":
            return
        name = getattr(getattr(ev, "entity", None), "name", "") or ""
        hid = _int(ev.value)
        if name and hid is not None:
            self._named_hero[name] = hid

    def _on_hide(self, ev) -> None:
        eid = ev.entity_id
        if eid is None:
            return
        e = self._entity(eid)
        for k, v in (ev.tags or {}).items():
            e.tags[k] = str(v)
        e.tags["ZONE"] = e.tags.get("ZONE", "SETASIDE")

    # ------------------------------------------------------------------
    # 待选 / 已选（饰品四选一、黑赐三选一、起手换牌）
    # ------------------------------------------------------------------

    def _on_choice(self, ev) -> None:
        """处理 `DebugPrintEntityChoices()` 流。

        ⚠️ 事件分三种，靠字段区分（选项行**不带** `choice_id`，这是实测形态）：
          1. 头行：`choice_title` 有值、`choice_option` 为空 → 开一个新块
          2. `Source=…`：`choice_source` 有值 → 补在本块上（决定是不是饰品/黑赐）
          3. `Entities[i]=`：`choice_option` 有值 → 加进当前块
        """
        # 1) 头行 —— `id=` 行是**唯一**能开新块的行
        #
        # ⚠️ 只有头行才收尾上一个块。`Source=` 与 `Entities[i]=` 行**不带 id**，
        # 它们属于"当前正在收选项的那个块"，看到就拿它们收尾会把刚开的块关掉
        # （实测踩过：Source 一来 `_choosing` 就变 None，后面所有选项全被丢）。
        if ev.choice_id is not None and ev.choice_option is None \
                and not ev.choice_source:
            # 旧块还没被选就来了新块 → 直接丢弃（不能留着挂到本局结束）
            self._choosing = PendingChoice(
                choice_id=ev.choice_id,
                kind=ev.choice_kind or "",
                player=ev.choice_player or "",
                expected=ev.choice_count,
            )
            self._state.pending_choice = None
            return

        if self._choosing is None:
            # 没见过头行就先来了选项/来源行（日志从中途开始）—— 认领不了，丢掉比乱挂好
            return

        # 2) Source=
        if ev.choice_source:
            ref = _source_ref(ev.choice_source)
            self._choosing.source_card_id = ref.card_id
            self._choosing.source_name = ref.name
            self._publish_choice()
            return

        # 3) 选项行
        o = ev.choice_option
        if o is not None:
            self._choosing.options.append(
                ChoiceOption(
                    index=o.index,
                    entity_id=o.entity_id,
                    name=o.name,
                    card_id=o.card_id,
                    zone=o.zone,
                )
            )
            self._publish_choice()

    def _publish_choice(self) -> None:
        """把当前待选挂到盘面上。

        ⚠️ **不能等"选项收全"再挂**：实测 `CountMax=1` 而选项有 3–4 个
        （`CountMin/CountMax` 指的是"选几个"，不是"给了几个"，用它是判不出收全的），
        而最后一个选项行之后**没有空行之类的结束标记**，收全瞬间根本收不到事件。
        所以只要来了一条选项就挂上引用 —— 对象是原地增补的，
        规则层读 `state` 时自然看到最新的完整列表。
        """
        pc = self._choosing
        if pc is None:
            return
        pc.is_local = self._choice_is_local(pc)
        self._attach_gifts(pc)
        self._state.pending_choice = pc

    def _attach_gifts(self, pc: "PendingChoice") -> None:
        """给每个选项补上它自带的黑暗之赐。

        ⚠️ 赐福实体和 `DARK_GIFT_ENTITY` 标签都在**待选块之前**就写好了
        （实测标签行比 `DebugPrintEntityChoices` 头行早 14 ms），所以这里读盘面
        一定读得到。反过来**不能等选完再回头查** —— 每局末态这些标签会被清成 0
        （实测 `HAS_DARK_GIFT` / `DARK_GIFT_ENTITY` 最后全是 0）。

        只有赐福卡牌库里才有中文名和词条（`BG36_MidGameEffect_000tNN`），
        这里只抄实体上的 `card_id`；名字与词条由规则层查卡表补齐。
        """
        for opt in pc.options:
            if opt.entity_id is None:
                continue
            ent = self._state.entities.get(opt.entity_id)
            if ent is None:
                continue
            gift_eid = _int(ent.tags.get("DARK_GIFT_ENTITY"))
            if not gift_eid:
                continue
            opt.gift_entity_id = gift_eid
            gift = self._state.entities.get(gift_eid)
            if gift is None:
                continue
            if gift.card_id:
                opt.gift_card_id = gift.card_id
            if gift.name and not gift.name.startswith("UNKNOWN ENTITY"):
                opt.gift_name = gift.name

    def _choice_is_local(self, pc: PendingChoice) -> bool:
        """这个待选是不是本地玩家的。

        `Player=` 给的是**账号名**（实测 `测试玩家#1234`），而本地玩家名是
        在跑盘面时从 `CURRENT_PLAYER` + 英雄实体反推出来的，所以两边要对名字。
        ⚠️ 名字还没认出来时走**实体归属退路**；两条路都不成立才返回 False
        （宁可漏报也不误报成"你的选择"）。
        """
        if not pc.player:
            return False
        if self._local_player_name:
            return pc.player == self._local_player_name
        # 名字还没锁定 —— 实测**开局英雄选择**就发生在这个窗口里：
        # `CURRENT_PLAYER` 切到本地玩家要等选完英雄（实测约 30–50 s 后），
        # 而三选一在开局约 0.6 s 就弹了。此时退一步用选项实体的控制者认。
        return self._options_belong_to_local(pc)

    def _options_belong_to_local(self, pc: PendingChoice) -> bool:
        """退路判据：选项实体是否**全部**归本地座位控制。

        实测英雄选项实体带 `CONTROLLER=1`，与本地账号玩家实体（CREATE_GAME
        里 `GameAccountId` 非 0 的那个）的 `CONTROLLER/PLAYER_ID` 一致。
        ⚠️ 要求"全部 + 每个都查得到控制者"：任何一个查不到（日志中途开始）
        就不认 —— 宁可漏报，也不把别人的选择挂成本地玩家的。
        """
        local_pid = self._resolve_local_player()
        if local_pid is None or not pc.options:
            return False
        for o in pc.options:
            if o.entity_id is None:
                return False
            ent = self._state.entities.get(o.entity_id)
            if ent is None:
                return False
            ctrl = ent.controller if ent.controller is not None else ent.player_id
            if ctrl != local_pid:
                return False
        return True

    def _on_choice_result(self, ev) -> None:
        """`SendChoices()` —— 玩家实际选了什么。

        ⚠️ 选项行同样不带 id，靠 `self._choosing` 认领；`_choosing` 为空时
        （比如日志从中途开始）静默忽略，不要乱记。
        """
        opt = ev.choice_option
        if opt is None:
            return  # 头行只表明"开始交答案了"，真正选了哪个在下一行
        if self._choosing is None:
            return
        self._commit_choice(opt)

    def _commit_choice(self, opt) -> None:
        """把「已选」落到盘面上：清掉待选，饰品的话记进 trinkets。"""
        pc = self._choosing
        chosen = ChoiceOption(
            index=opt.index, entity_id=opt.entity_id, name=opt.name,
            card_id=opt.card_id, zone=opt.zone,
        )
        self._state.last_choice = chosen
        if pc is not None:
            if pc.is_trinket and chosen.card_id:
                self._add_trinket(pc.trinket_size, chosen)
            elif pc.is_dark_gift:
                self._state.dark_gifts.append(
                    DarkGiftRecord(
                        minion_name=chosen.name,
                        minion_card_id=chosen.card_id,
                        minion_entity_id=chosen.entity_id,
                        choice_id=pc.choice_id,
                    )
                )
            # ⚠️ 起手换牌（MULLIGAN）不记进 choice_log：它只会把日志刷乱，
            # 而且规则引擎完全不看它。
            if pc.kind != "MULLIGAN":
                self._state.choice_log.append(
                    ChoiceRecord(
                        choice_id=pc.choice_id,
                        kind=pc.kind,
                        source_name=pc.source_name or pc.source_card_id,
                        selected_name=chosen.name,
                        selected_card_id=chosen.card_id,
                    )
                )
        self._state.pending_choice = None
        self._choosing = None

    def _add_trinket(self, size: str, chosen) -> None:
        if not chosen.card_id:
            return
        name = chosen.name
        for t in self._state.trinkets:
            if t.size == size:
                t.card_id, t.name = chosen.card_id, name or t.name
                t.entity_id = chosen.entity_id
                return
        self._state.trinkets.append(
            Trinket(size=size, card_id=chosen.card_id, name=name,
                    entity_id=chosen.entity_id)
        )

    @staticmethod
    def _is_equipped_trinket(opt) -> bool:
        return (opt.card_id.startswith(("BG30_MagicItem", "BG32_MagicItem",
                                        "BG36_MagicItem"))
                and opt.zone == "PLAY")

    @staticmethod
    def _trinket_size_of(card_id: str) -> str:
        return "large" if card_id.endswith(("_2nd", "2nd")) else "small"

    # ------------------------------------------------------------------
    # 索引维护
    # ------------------------------------------------------------------
    def _entity(self, eid: int) -> Entity:
        e = self._state.entities.get(eid)
        if e is None:
            e = Entity(entity_id=eid)
            self._state.entities[eid] = e
        return e

    def _index_entity(self, e: Entity) -> None:
        """把实体登记进座位 / 英雄表。"""
        pid = e.player_id
        if pid is None:
            return
        if e.card_id and "HERO" in e.card_id.upper() and e.card_id != CARD_BOB:
            self._hero_by_player[pid] = e.entity_id
            # 账号实体上还没有 `HERO_ENTITY`（属性行还没落地）时，用「账号实体 ↔
            # 英雄实体」的 CONTROLLER 直接补上本地玩家的 PLAYER_ID，否则第二局
            # `_resolve_local_player()` 会返回 None、座位上 `is_local` 全为 False。
            if self._local_account_player is not None:
                pe = self._state.entities.get(self._local_account_player)
                if (pe is not None and "HERO_ENTITY" not in pe.tags
                        and pe.controller is not None and pe.controller == e.controller):
                    self._local_account_player_hero = e.entity_id
            return
        # 英雄的 `PLAYER_ID` 在**属性行**里（`tag=PLAYER_ID`），而 `index_entity` 可能
        # 在 card_id 还没落到实体上时就被调用（例如先来 `TAG_CHANGE … tag=PLAYER_ID`
        # 再来 `FULL_ENTITY`）。已登记的同一个 entity_id 直接刷新，避免座位表空掉。
        if self._hero_by_player.get(pid) == e.entity_id:
            self._hero_by_player[pid] = e.entity_id
    def _ensure_seat(self, pid: int) -> Seat:
        seat = self._state.seats.get(pid)
        if seat is None:
            seat = Seat(player_id=pid)
            self._state.seats[pid] = seat
        return seat

    def _player_id_of(self, ev) -> Optional[int]:
        """事件指向的玩家 PLAYER_ID（先按实体 id，再按名字）。"""
        eid = getattr(ev, "entity_id", None)
        if eid is not None:
            e = self._state.entities.get(eid)
            if e is not None and e.player_id is not None:
                return e.player_id
            for pid, hid in self._hero_by_player.items():
                if hid == eid:
                    return pid
        name = ""
        if getattr(ev, "entity", None) is not None:
            name = getattr(ev.entity, "name", "") or ""
        if name:
            for e in self._state.entities.values():
                if e.name == name and e.player_id is not None:
                    return e.player_id
        return None

    def _is_game_entity(self, ev) -> bool:
        ent = getattr(ev, "entity", None)
        return bool(ent is not None and getattr(ent, "name", "") == "GameEntity")

    # ------------------------------------------------------------------
    # 派生快照
    # ------------------------------------------------------------------
    def _refresh_shop(self) -> bool:
        # 座位数变了说明本轮刷新改动了别的视图，`_refresh` 要再跑一轮收敛。
        before = len(self._state.seats)
        minions: List[ShopSlot] = []
        spells: List[ShopSlot] = []
        for e in self._state.entities.values():
            if e.card_id == CARD_DRAG_BUY:
                kind = "minion"
            elif e.card_id == CARD_DRAG_BUY_SPELL:
                kind = "spell"
            else:
                continue
            ref = _int(e.tags.get(TAG_SHOP_SLOT_REF))
            slot = ShopSlot(slot_entity_id=e.entity_id,
                            minion_entity_id=ref if ref else None,
                            controller=e.controller,
                            kind=kind)
            if ref:
                m = self._state.entities.get(ref)
                if m is not None:
                    slot.card_id = m.card_id
                    slot.name = m.name
                    slot.tech_level = m.tech_level
                    slot.attack = m.attack
                    slot.health = m.health
                    slot.golden = m.golden
                    # 法术的买价：槽位实体自带 `COST`（实测 1~4）。这里只抄原始
                    # 值，规则层有卡表时会用卡表的 `cost` 覆盖它（更权威）。
                    if kind == "spell":
                        slot.cost = _int(e.tags.get("COST"))
            (spells if kind == "spell" else minions).append(slot)
        # ⚠️ 归属必须在**同一次** `_refresh_shop` 里算完：`_refresh()` 的收敛
        # 循环靠 `changed` 决定要不要再跑一轮，而"归属变了"不改变槽位集合、
        # 无法表达为 changed。拆成两处（集合在 `_refresh_shop`、归属在别处/上一轮）
        # 会永久停在错误值（实测：本地槽位 `belongs_to_local=False`）。
        local_ctrl = self._local_controller()
        for slot in minions + spells:
            if local_ctrl is not None and slot.controller == local_ctrl:
                slot.belongs_to_local = True
        minions.sort(key=lambda s: s.slot_entity_id)
        spells.sort(key=lambda s: s.slot_entity_id)
        self._state.shop = minions
        self._state.spells = spells
        return len(self._state.seats) != before

    def _refresh_boards(self) -> bool:
        before = (len(self._state.board), len(self._state.hand))
        local_pid = self._state.local_player_id
        if local_pid is None:
            local_pid = self._resolve_local_player()
            self._state.local_player_id = local_pid
        local_ctrl = self._local_controller()
        board, hand = [], []
        for e in self._state.entities.values():
            if not e.card_id:
                continue
            if e.card_id.startswith(("TB_BaconShop", "Bacon_", "BG30_Trinket",
                                     "BG36_Button", "BG_OldGod")):
                continue
            if e.zone == "PLAY" and e.card_type == "MINION":
                if local_ctrl is not None and e.controller is not None \
                        and e.controller != local_ctrl:
                    continue
                board.append(e)
            elif e.zone == "HAND":
                hand.append(e)
        board.sort(key=lambda e: (e.controller or 0, e.zone_pos or 0))
        self._state.board = board
        self._state.hand = hand
        return (len(board), len(hand)) != before

    def _resolve_local_player(self) -> Optional[int]:
        """本地玩家的 PLAYER_ID。

        优先用带真实 GameAccountId 的那个 Player 实体的 HERO_ENTITY，
        再沿 HERO_ENTITY 找英雄实体上的 PLAYER_ID。
        """
        if self._local_account_player_hero is not None:
            he = self._state.entities.get(self._local_account_player_hero)
            if he is not None and he.player_id is not None:
                return he.player_id
        if self._local_account_player is not None:
            pe = self._state.entities.get(self._local_account_player)
            if pe is not None:
                hero_id = _int(pe.tags.get("HERO_ENTITY"))
                if hero_id is not None:
                    he = self._state.entities.get(hero_id)
                    if he is not None and he.player_id is not None:
                        return he.player_id
                if pe.player_id is not None:
                    return pe.player_id
        # 退路：柜台实体 TB_BaconShop_8P_PlayerE 的 CONTROLLER
        for e in self._state.entities.values():
            if e.card_id == CARD_LOCAL_PLAYER_ENCH and e.controller is not None:
                for hero in self._state.entities.values():
                    if hero.player_id is not None and hero.controller == e.controller \
                            and hero.card_type == "HERO":
                        return hero.player_id
        return self._state.local_player_id

    def _local_controller(self) -> Optional[int]:
        """本地英雄实体的 `CONTROLLER`（= 本地玩家的 player_id）。

        这是区分"自家随从 / 自家商店槽位"与对手的唯一可靠判据（P1 实测：商店与
        自家场面同在 `ZONE=PLAY`，`CONTROLLER` 才是分界线）。
        """
        pid = self._state.local_player_id
        if pid is None:
            return None
        seat = self._state.seats.get(pid)
        if seat is None or seat.hero_entity_id is None:
            return None
        hero = self._state.entities.get(seat.hero_entity_id)
        return hero.controller if hero is not None else None

    def _refresh_seats(self) -> bool:
        # ⚠️ 必须自己解析一次，不能沿用 `state.local_player_id`：`_refresh` 的收敛
        # 循环在"没有改动"时会提前 break，可能导致 `_refresh_boards` 从没跑过，
        # 本地玩家 id 仍是 None，座位上的 `is_local` 就全是 False（实测翻车）。
        before = len(self._state.seats)
        local_pid = self._resolve_local_player()
        self._state.local_player_id = local_pid
        for pid, hid in self._hero_by_player.items():
            seat = self._ensure_seat(pid)
            seat.hero_entity_id = hid
            hero = self._state.entities.get(hid)
            if hero is not None:
                seat.hero_card_id = hero.card_id
                seat.hero_name = hero.name
                seat.tech_level = hero.tech_level
                # ⚠️ 英雄实体的 HEALTH 是"上限"（40/30…），当前血量 = HEALTH - DAMAGE
                seat.max_health = _int(hero.tags.get("HEALTH")) or seat.max_health
                seat.damage = _int(hero.tags.get("DAMAGE")) or seat.damage
                seat.health = seat.effective_health
                seat.max_tech_level = _int(hero.tags.get("BACON_MAX_PLAYER_TECH_LEVEL"))
                seat.died_last_combat = str(hero.tags.get("BACON_DIED_LAST_COMBAT", "0")) not in ("0", "")
                seat.won_last_combat = str(hero.tags.get("BACON_WON_LAST_COMBAT", "0")) not in ("0", "")
            seat.is_local = (local_pid is not None and pid == local_pid)
        # 血量/金币挂在"玩家实体"上（`Player EntityID=N` 那条），
        # 通过 HERO_ENTITY 反向关联。⚠️ 这些 `Player` 实体本身没有 HEALTH/DAMAGE
        # （实测只有 PLAYER_ID/HERO_ENTITY/PLAYER_TECH_LEVEL），所以这里只补经济，
        # **不能**把 `health` 写回去覆盖上面由英雄实体算出的有效血量。
        for e in self._state.entities.values():
            he = _int(e.tags.get("HERO_ENTITY"))
            if he is None or e.player_id is not None:
                continue
            target = None
            for pid, hid in self._hero_by_player.items():
                if hid == he:
                    target = self._ensure_seat(pid)
                    break
            if target is None:
                continue
            if e.tags.get("RESOURCES") is not None:
                target.resources = _int(e.tags.get("RESOURCES"))
            if e.tags.get("RESOURCES_USED") is not None:
                target.resources_used = _int(e.tags.get("RESOURCES_USED"))
        return len(self._state.seats) != before
