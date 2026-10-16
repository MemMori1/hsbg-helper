#!/usr/bin/env python3
"""P3：规则引擎（硬指标）——**完全不依赖任何模型**。

设计原则（ADR-2）：能用规则算的绝不用模型，模型只做无法量化的判断题。
这一层是质量地基，所以每个规则都必须能**离线单测**：喂 `GameState` → 断言打分。

分层职责::

    GameState（state.py）  +  CardDB（cards.py）
                    ↓
              RulesEngine.evaluate()
                    ↓
              AdviceReport（结构化，可 JSON）

产出的是**建议**不是动作：本项目红线是"只看不动"（需求文档 §2.3），
这一层连执行接口都不留。

数据来源全部实测（见 docs/需求分析.md §11）：

- 升本费用：**直接读 `TB_BaconShopTechUp0X_Button` 的 `COST`**，这比任何硬编码
  的表都准。实测同一档位在不同对局/不同回合是 5/6/7、9/10/11 不等（提前升本会
  便宜、上回合输了会打折），只有按钮上的数字是当前真值。
  ⚠️ 取按钮时必须限定 `ZONE=PLAY`：`SETASIDE`/`REMOVEDFROMGAME` 里的是陈旧值。
- 刷新费用：`TB_BaconShop_8p_Reroll_Button` 的 `COST`（0 = 本回合有免费刷新）。
- 免费刷新次数：`Bacon_Free_Refresh_Player_Ench` 的 `BACON_FREE_REFRESH_COUNT`。
- 三连：自家场面 + 手牌的 `CardID` 重复度；场上已有金色随从则不再提示。
- 血量：`Seat.effective_health`（英雄 `HEALTH` − `DAMAGE`）。
"""
from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .cards import Card, CardDB
from .state import Entity, GameState, Seat, ShopSlot
from .heroes import HeroStats
from .trinkets import TrinketStats

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 升本按钮的 CardID 前缀（`TB_BaconShopTechUp02_Button` → 升到 2 本）
TECHUP_BUTTON_PREFIX = "TB_BaconShopTechUp"
REROLL_BUTTON_CARD = "TB_BaconShop_8p_Reroll_Button"
FREE_REFRESH_ENCH = "Bacon_Free_Refresh_Player_Ench"
TRINKET_CARDS = ("BG30_Trinket_1st", "BG30_Trinket_2nd")
#: 「黑暗发现」按钮（`BG36_Button_DarkGift`）——点它能三选一拿一个「黑暗之赐」。
#: 实测这个机制**有日志可判**：按钮上 `BACON_DARK_GIFT_PRESSABLE_VFX=1` 表示现在可点，
#: 拿到赐福的随从带 `HAS_DARK_GIFT=1`，`DARK_GIFT_ENTITY` 指向赐福本体
#: （`BG36_MidGameEffect_000tNN`，如 t13=鹰身人之爪、t18=不断复制、t11=双重幻象）。
DARK_GIFT_BUTTON_CARD = "BG36_Button_DarkGift"
#: 随从身上的「有黑暗之赐」标 / 指向赐福实体的引用标
DARK_GIFT_TAGS = ("HAS_DARK_GIFT", "DARK_GIFT_ENTITY")

#: ⭐ 基准升本曲线：(回合, 这个回合**应该**已经到的酒馆等级)。
#:
#: 依据公开的升节奏打法：**四费升二本**是绝对主流（约七成对局），
#: 之后按本数递进（三本 → 四本 → 五本 → 六本）。
#: 这条曲线是**决策框的默认答案**：到点就劝升，只有明确理由才偏离，
#: 而且偏离时必须在 `DecisionBox.reason` 里说清为什么（见 `_level_guard`）。
#:
#: ⚠️ 曲线刻意比最激进的打法晚一轮 —— 真机对局里达标的玩家在 3 本停到回合 12
#: 才升 4 本，硬曲线会让提示一直和玩家实际选择相反。
LEVEL_CURVE: Tuple[Tuple[int, int], ...] = (
    (3, 2), (7, 3), (10, 4), (13, 5), (16, 6),
)

#: 随从的"关键词价值"权重（用于场面强度打分）。
#: ⚠️ 毒、圣盾、嘲讽这类价值高；风怒/复生次之。数值是启发式，不是官方数据。
MECHANIC_WEIGHT: Dict[str, float] = {
    "POISONOUS": 3.0, "VENOMOUS": 3.0, "DIVINE_SHIELD": 2.0,
    "TAUNT": 0.5, "REBORN": 1.5, "WINDFURY": 1.5,
    "DEATHRATTLE": 0.8, "BATTLECRY": 0.6, "AVENGE": 1.0,
    "SPELLCRAFT": 0.8, "WINDFURY_ATTACK": 1.0,
    # 「进击」（BACON_RALLY）实测有 146 张卡带这个 mechanics 标，但之前漏了。
    "BACON_RALLY": 1.2,
    # 「发动」——**不在 mechanics 字段里**，只能从卡牌正文的模板文字抠出来。
    # 见 `parse_text_keywords()`；费用越低越强，所以按费用分档给权重。
    "发动(0)": 1.6, "发动(1)": 1.2, "发动(2)": 0.9,
    #: 兜底：卡文里有「发动」但抠不出费用时
    "发动": 1.0,
    # 「塑造法术」在卡表里是作弊字段 `BACON_SPELLCRAFT_ID`（值是数字 id），
    # 不是 mechanics 标，这里同样按卡文兜底。
    "塑造法术": 0.8,
}

#: 酒馆法术按**效果**分类（实测卡表里 208 张 `BATTLEGROUND_SPELL`，其中真正能在
#: 商店里买到的、两份真机日志里出现过 40 种）。
#:
#: ⚠️ 分类只用来调**法术内部**的相对顺序：法术起手分和随从"按身材算"的分不是
#: 同一把尺子（实测直接把两者比大小会让决策框在回合 1 空场去买法术）。
#: ⚠️ 顺序有意义：`给牌` 先匹配，避免"发现一张亡语随从牌"被同时算成 `给关键词`。
SPELL_EFFECT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("给牌", r"发现|获取|复制|偷取"),
    ("涨钱上限", r"铸币上限"),
    ("给钱", r"获得\d*枚铸币"),
    ("刷新", r"刷新|锁入"),
    ("变身", r"变形|异变|点金|变为金色"),
    ("给关键词", r"嘲讽|圣盾|复生|风怒|潜行|剧毒|亡语|进击|战斗开始时|回合结束时|施放"),
    ("给身材", r"\+\d+/\+\d+|属性值"),
)

#: 效果权重（叠在法术起手分上）。口径：
#:  · `给牌` ≈ 白拿一张随从，最稳（主厨甄选、发现一个等级N的随从）
#:  · `涨钱上限` = 每回合永久 +1 金，越早越值（钻探原油）
#:  · `给钱` **只给一点点**：实测「酒馆币」是 1 金买 1 金、净 0，
#:    「慎重投资」1 金换下回合 2 金、净 +1 —— 价值远低于白拿一张随从
#:  · `变身/异常` 方差大，样本不够时不赌
SPELL_EFFECT_WEIGHT: Dict[str, float] = {
    "给牌": 2.5,
    "涨钱上限": 2.0,
    "刷新": 1.0,
    "给身材": 1.0,
    "给钱": 0.5,
    "给关键词": 0.5,
    "变身": -1.0,
}

#: 法术的星级系数**比随从低**（随从用 1.5）。星级只代表"在第几级酒馆出现"，
#: 不等于效果价值 —— 用 1.5 会让 5 星「变身金色」压过 2 星「白拿一张随从」。
SPELL_TECH_LEVEL_WEIGHT = 1.0

#: 「使**你的**随从/每个/所有」= 全局增益，价值随场面走；
#: 「使**一个**随从」= 单体，场上没随从时根本没处贴。
_SPELL_GLOBAL_RE = re.compile(r"你的随从|你的战队|每个|所有|酒馆中")
_SPELL_SINGLE_RE = re.compile(r"使一个(友方)?随从|选择一个随从|使一条龙")

_KEYWORD_MECHANICS = ("POISONOUS", "VENOMOUS", "DIVINE_SHIELD", "TAUNT",
                      "REBORN", "WINDFURY", "DEATHRATTLE", "BATTLECRY",
                      "AVENGE", "SPELLCRAFT", "BACON_RALLY")

#: 卡文里带费用参数的模板关键词。
#: ⚠️ 必须是**白名单**。实测卡文里 `随从消耗(1)`、`出售获得(0)`、`铸币减少(2)`
#: 这类文字也长得跟"关键词（数字）"一模一样，用宽松的 `[\u4e00-\u9fff]{2,4}[（(]\d+[）)]`
#: 会一口气捞进 100 张风马牛不相及的卡（异变、英雄技能、饰品…），把商店打分带歪。
_TEXT_KEYWORDS_WITH_COST = ("发动", "复仇", "塑造法术")

#: `发动（2）：` 这种"关键词（费用）"。全角括号是实测形态，半角一并容忍。
_TEXT_KEYWORD_RE = re.compile(
    r"(" + "|".join(_TEXT_KEYWORDS_WITH_COST) + r")\s*[（(](\d+)[）)]")

#: 卡文里没有费用参数的关键词（整词匹配）。
#: ⚠️ 同样只放"确定是机制名"的词；`黑暗之赐` 是由另外的卡赋予的，不在随从卡文里。
_TEXT_KEYWORDS = ("进击",)

#: 关键词的中文显示名（面板/CLI 用）。
MECHANIC_ZH: Dict[str, str] = {
    "POISONOUS": "剧毒", "VENOMOUS": "烈毒", "DIVINE_SHIELD": "圣盾",
    "TAUNT": "嘲讽", "REBORN": "复生", "WINDFURY": "风怒",
    "DEATHRATTLE": "亡语", "BATTLECRY": "战吼", "AVENGE": "复仇",
    "SPELLCRAFT": "塑造法术", "BACON_RALLY": "进击",
    # 卡文模板关键词（`mechanics` 字段里没有）
    "发动": "发动", "进击": "进击", "复仇": "复仇",
}


#: 中文模板关键词 → 等价的英文 mechanics 标。
#: ⚠️ 必须去重，否则同一机制会进两次分：实测 `BG21_001`（苍鳞鳄鱼）既有
#: `mechanics: ["AVENGE", ...]`，正文又写 `复仇（2）`，不去重就是 1.0 + 1.2 = 2.2。
_TEXT_TO_MECHANIC: Dict[str, str] = {
    "复仇": "AVENGE", "进击": "BACON_RALLY", "塑造法术": "SPELLCRAFT",
}

#: ⚠️ 卡表 `mechanics` 里塞了大量**内部占位符**，绝不能直接喂给模型或参与打分。
#: 实测 5,787 张战棋卡里 `TRIGGER_VISUAL` 有 **1,410 张**（占 24%！），
#: 它只表示"这张卡有别的东西要触发"，不是游戏机制；`ENCHANTMENT_INVISIBLE`、
#: `InvisibleDeathrattle`、`TAG_ONE_TURN_EFFECT` 同理。早期 `advisor.py` 的
#: evidence 直接用了原始 `card.mechanics` → 模型看到的商店里全是 `TRIGGER_VISUAL`，
#: 而规则层用的是白名单，两边对同一张卡的描述不一致。
_MECHANIC_NOISE = frozenset((
    "TRIGGER_VISUAL", "ENCHANTMENT_INVISIBLE", "InvisibleDeathrattle",
    "TAG_ONE_TURN_EFFECT",
))


def clean_mechanics(mechanics: Sequence[str]) -> Tuple[str, ...]:
    """把卡表原始 `mechanics` 过滤成"真的能影响决策"的那些。

    ⚠️ 这是 `advisor.py`（喂模型）与 `rules.py`（打分）的**共同口径**，
    两边必须一致 —— 否则同一张卡在题面里和在打分理由里描述得不一样。
    """
    out: List[str] = []
    for m in mechanics or ():
        if not m or m in _MECHANIC_NOISE:
            continue
        if m not in out:
            out.append(m)
    return tuple(out)


def parse_text_keywords(text: str) -> List[str]:
    """从卡牌正文里抠出 `mechanics` 字段**没有**的机制关键词。

    为什么需要这个：实测 `BG36_099`（蚀脑怪）的 `mechanics` 是 `None`，但正文写着
    `<b>发动（0）：</b>弃掉一张牌…` —— 「发动」根本不是 mechanics 标，只存在于模板
    文字里。全库 50 张卡（含金色版）都是这个形态，靠 `mechanics` 一律扫不到。

    返回形如 `["发动(0)"]` 的键（费用越低越强，所以在键里带上费用），
    也接受 `["进击"]` 这种没有费用的。
    """
    if not text:
        return []
    out: List[str] = []
    parameterized: List[str] = []
    for kw, cost in _TEXT_KEYWORD_RE.findall(text):
        key = f"{kw}({cost})"
        if key not in out:
            out.append(key)
        parameterized.append(kw)
    for kw in _TEXT_KEYWORDS:
        # 已经带参数抠到过（如 `复仇(3)`）就不再补一个裸词，否则同一机制进两次分
        if kw in text and kw not in out and kw not in parameterized:
            out.append(kw)
    return out

#: 血量低于这个值就报"危险"
LOW_HEALTH_THRESHOLD = 15
#: 血量低于这个值算"濒死"
CRITICAL_HEALTH_THRESHOLD = 8

#: 卡牌类型里不算"随从"的
NON_MINION_TYPES = ("SPELL", "HERO", "HERO_POWER", "ENCHANTMENT",
                    "BATTLEGROUND_TRINKET", "GAME", "PLAYER")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RulesConfig:
    """可调参数。默认值尽量取实测/社区共识值，但不保证与官方一致。"""
    #: 升本费用兜底表（tier → 费用）。**优先用按钮上的实时 `COST`**，这里只在
    #: 按钮缺失（日志不全 / 手搓状态测试）时兜底。
    tech_up_costs: Dict[int, int] = field(default_factory=lambda: {
        2: 5, 3: 7, 4: 8, 5: 9, 6: 10, 7: 11,
    })
    #: 买一个随从的基准费用（战棋固定 3 金）
    buy_cost: int = 3
    #: 刷新费用
    reroll_cost: int = 1
    #: 场面随从数上限（不算金色召唤物），超过就提示"该卖了"
    board_limit: int = 7
    #: 手牌上限
    hand_limit: int = 10
    #: 低于这个血量开始报血线预警
    low_health: int = LOW_HEALTH_THRESHOLD
    critical_health: int = CRITICAL_HEALTH_THRESHOLD
    #: 战斗伤害上限（会从盘面的 `BACON_COMBAT_DAMAGE_CAP` 覆盖）
    combat_damage_cap: Optional[int] = None


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------

@dataclass
class ShopItem:
    """一个商店槽位的打分结果。"""
    index: int                   # 在列表里的序号（1 起）
    slot_entity_id: int
    card_id: str
    name: str
    tech_level: Optional[int]
    attack: Optional[int]
    health: Optional[int]
    golden: bool
    cost: int
    score: float
    action: str                  # 买 / 考虑 / 跳过
    reasons: List[str] = field(default_factory=list)
    completes_triple: bool = False
    makes_pair: bool = False
    race: str = ""
    #: 中文关键词标签（剧毒/圣盾/嘲讽/发动/进击…），供面板显示
    keywords: List[str] = field(default_factory=list)
    #: `"minion"` 或 `"spell"` —— 法术没有攻/血，费用也是自己的
    kind: str = "minion"
    #: 法术卡文（已压平标签），供面板/模型看
    text: str = ""

    def describe(self) -> str:
        return f"{self.name} {self.score:.1f}分 {self.action}"


@dataclass
class TripleCandidate:
    """差一张就三连的候选。"""
    card_id: str
    name: str
    owned: int                   # 已有张数（场面 + 手牌）
    in_shop: bool = False        # 商店里能直接买到
    tech_level: Optional[int] = None

    @property
    def needed(self) -> int:
        return max(0, 3 - self.owned)


@dataclass
class LevelAdvice:
    """升本建议。"""
    current_tier: Optional[int]
    next_tier: Optional[int]
    cost: Optional[int]
    affordable: bool
    score: float
    action: str                  # 升本 / 观望 / 不升
    reasons: List[str] = field(default_factory=list)


@dataclass
class Warning:
    level: str                   # info / warn / danger
    text: str


@dataclass
class DecisionBox:
    """「现在该做什么」—— **一个**动作 + 关键数值 + 一行理由。

    设计意图：商店打分、升本建议、预警这些都是**分析**，玩家还是得自己权衡。
    决策框把分析收敛成**决定**，让玩家不用自己判断。所以这里只给一个动作，
    理由最多一行。

    动作取值：`选择` / `升本` / `买` / `刷新` / `攒钱` / `观望`。
    """
    action: str = ""
    #: 动作的对象：升本是 `"3本"`，买牌是卡名，选择是题面
    target: str = ""
    #: 这个动作要花的金币（`选择` 是 0，`观望` 是 None）
    cost: Optional[int] = None
    gold: Optional[int] = None
    reason: str = ""
    #: `now` = 现在立刻做（免费收益，别忘）；`soon` = 这回合内做；`wait` = 先别花
    urgency: str = "soon"
    #: 曲线在这个回合要求的目标本数（没有就是 None）
    curve_tier: Optional[int] = None
    #: 是否偏离了 `LEVEL_CURVE`
    off_curve: bool = False
    #: 触发原因（机器可读）：pending_choice / dark_gift / curve / curve_blocked /
    #: triple / shop / thin_board / save_for_level / free_refresh / idle
    trigger: str = ""

    @property
    def headline(self) -> str:
        """面板大字用的短标题。"""
        if self.action == "买" and self.target:
            return f"买 {self.target}"
        if self.action == "升本":
            return f"升本 {self.target}" if self.target else "升本"
        if self.action == "选择":
            return "先选牌"
        if self.action == "升不起":
            return "升不起"
        if self.action == "刷新":
            return "刷新"
        return self.action or "观望"

    @property
    def cost_text(self) -> str:
        if self.cost is None:
            return ""
        return f"{self.cost} 金"


@dataclass
class DarkGift:
    """「黑暗之赐」的一次可用状态。

    实测机制：`黑暗发现` 按钮带 `BACON_DARK_GIFT_PRESSABLE_VFX=1` 时表示现在能点，
    点了之后三选一。赐福本身是卡（`BG36_MidGameEffect_000tNN`），会挂在随从上
    （随从侧 `HAS_DARK_GIFT=1` + `DARK_GIFT_ENTITY=<赐福实体 id>`）。
    """
    available: bool = False          # 现在能点「黑暗发现」吗
    button_entity_id: Optional[int] = None
    #: 场上带黑暗之赐的自家随从名（`HAS_DARK_GIFT=1` 的那些）
    granted: List[str] = field(default_factory=list)
    #: 已知赐福名（从 `DARK_GIFT_ENTITY` 指向的实体卡表名解析）
    known_effects: List[str] = field(default_factory=list)


def _clean_text(text: str, limit: int = 60) -> str:
    """去掉卡文里的 `<b>` 这类标记并压平空白（赐福卡文里换行很常见）。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass
class ChoiceItem:
    """待选列表里的一项（带卡表补出来的信息）。"""
    index: int
    card_id: str = ""
    name: str = ""
    tech_level: Optional[int] = None
    attack: Optional[int] = None
    health: Optional[int] = None
    text: str = ""
    kind: str = ""          # trinket / minion / other
    note: str = ""          # 从卡文/身材提炼的一行特征提示（不做打分）
    #: 英文名/英文卡文 —— 只用来喂 JevK5（ADR-4：题面与 evidence 必须英文）。
    #: ⚠️ 不能用中文名去英文卡表里反查：中文 `雷诺·杰克逊` 对应英文
    #: `Reno Jackson`，而日志里的皮肤短名是第三套写法，匹配不可靠。
    #: 正解是靠 **CardID** 去英文卡表取，CardID 是语言无关的。
    name_en: str = ""
    text_en: str = ""

    #: HSReplay 实证分（**英雄与饰品选项**有，见 `heroes.HeroStats.score` /
    #: `trinkets.TrinketStats.score`）。
    #: `None` = 没有数据（非英雄/饰品选项 / 新补丁刚出还没统计到）——规则层对
    #: "没有数据"必须保持沉默，不能当 0 分（那会让新选项看起来比最差的还差）。
    score: Optional[float] = None
    #: 一行实证摘要（中文展示）：饰品 `A级 · 选取43% · 平均名次3.90`；
    #: 英雄 `选取率78% · 护甲6`
    score_note: str = ""
    #: 同上（英文，喂模型）——ADR-4：题面与 evidence 必须全英文
    score_note_en: str = ""

    #: 黑暗之赐的**赐福**本身（另一张卡，`BG36_MidGameEffect_000tNN`）。
    #: 黑赐三选一里选项是随从，但玩家真正在挑的是赐福 —— 同一只随从可以配
    #: 完全不同的词条（实测 `鹰身人之爪` 给 圣盾+风怒、`防御献祭` 给 +10 血+亡语）。
    #: ⚠️ 赐福关系写在 `DARK_GIFT_ENTITY` 标签上，而该标签**每局末态会被清 0**，
    #: 所以必须在选项挂上盘面时抄一份（`StateBuilder._attach_gifts` 干这事）。
    gift_card_id: str = ""
    gift_name: str = ""
    gift_text: str = ""
    gift_name_en: str = ""
    gift_text_en: str = ""

    @property
    def label(self) -> str:
        return self.name or self.card_id or "?"

    @property
    def label_en(self) -> str:
        """喂模型用的名字：英文卡表名 → 中文名 → CardID。"""
        return self.name_en or self.name or self.card_id or "?"

    @property
    def gift_label(self) -> str:
        """展示用的赐福名（中文优先）。"""
        return self.gift_name or self.gift_card_id or ""

    @property
    def gift_label_en(self) -> str:
        """喂模型用的赐福名（英文卡表名优先）。"""
        return self.gift_name_en or self.gift_name or self.gift_card_id or ""

    @property
    def gift_note(self) -> str:
        """展示用的一行赐福摘要：`鹰身人之爪（圣盾。风怒）`。"""
        name = self.gift_label
        if not name:
            return ""
        text = _clean_text(self.gift_text)
        return "%s（%s）" % (name, text) if text else name

    @property
    def gift_note_en(self) -> str:
        """喂模型用的一行赐福摘要（英文）。"""
        name = self.gift_label_en
        if not name:
            return ""
        text = _clean_text(self.gift_text_en or self.gift_text)
        return "%s (%s)" % (name, text) if text else name

    @property
    def has_gift(self) -> bool:
        return bool(self.gift_card_id or self.gift_name)

    @property
    def note_en(self) -> str:
        """喂模型用的特征行（英文卡文优先，退回中文卡文）。

        ⚠️ 黑暗之赐必须把**赐福**写进来：实测同一只随从会配不同赐福，
        "这个选项强不强"主要看赐福给了什么（圣盾/风怒/复生/亡语…），
        只报随从的卡文等于把最关键的那半条信息丢了。
        ⚠️ HSReplay 实证统计（饰品选项）同理排在卡文**前面** —— 卡文可能很长
        （截到 140 字），统计被挤到后面就等于没给。
        """
        bits: List[str] = []
        gift = self.gift_note_en
        if gift:
            bits.append("Dark Gift: " + gift)
        if self.score_note_en:
            bits.append(self.score_note_en)
        text = _clean_text(self.text_en or self.text, limit=140)
        if text:
            bits.append(text)
        return "  ".join(bits)


@dataclass
class ChoiceAdvice:
    """一次等待玩家做的选一（英雄选择 / 饰品四选一 / 黑赐三选一）。

    ⚠️ 规则层的打分能力**只对英雄与饰品**有：HSReplay 提供了这两类的实证统计
    （`heroes.py` 选取率、`trinkets.py` tier+平均名次），所以它们会给出明确推荐。
    赐福与"卡牌效果触发的发现"（三连奖励、随从战吼…）仍然**不打分** ——
    它们没有可复现的样本，继续只做"列清楚是什么 + 一行特征提示"。
    """
    kind: str = ""                  # hero / trinket / trinket_large / dark_gift / mulligan / other
    title: str = ""
    source_name: str = ""
    source_text: str = ""           # 触发这次选择的卡文（三连奖励 / 某饰品的效果等）
    title_en: str = ""              # 英文标题（喂模型）
    source_text_en: str = ""        # 英文触发源卡文（喂模型）""
    is_local: bool = True
    options: List[ChoiceItem] = field(default_factory=list)
    #: 规则层明确推荐的那一项（目前只有饰品四选一，按 HSReplay 实证分选）。
    #: `None` = 没有可用的实证数据，退回"只展示不推荐"（老行为）。
    recommended: Optional[ChoiceItem] = None
    #: 推荐的完整性提示（如 `2 项无实证数据`）；没有缺口时为空。
    recommend_note: str = ""

    @property
    def recommend_text(self) -> str:
        """一行推荐文案（CLI / 决策框 / 预警共用）；无推荐时为空串。"""
        if self.recommended is None:
            return ""
        text = "规则推荐 %s" % self.recommended.label
        if self.recommended.score_note:
            text += "（%s）" % self.recommended.score_note
        if self.recommend_note:
            text += "；" + self.recommend_note
        return text

    @property
    def display_title(self) -> str:
        if self.kind == "hero":
            return "英雄选择"
        if self.kind == "trinket":
            return "小饰品四选一"
        if self.kind == "trinket_large":
            return "大饰品四选一"
        if self.kind == "dark_gift":
            return "黑暗之赐三选一"
        # 其余是卡牌效果触发的发现（三连奖励、饰品效果、随从战吼…）——
        # 标题用触发源的名字更有用（实测有 `三连奖励` / `死亡护命匣` / `无限潜力`）。
        if self.title:
            return "选择：" + self.title
        return "待选"


@dataclass
class TrinketInfo:
    """已装备的饰品（面板/CLI 用）。"""
    size: str = ""          # small / large
    card_id: str = ""
    name: str = ""
    text: str = ""
    tech_level: Optional[int] = None

    @property
    def label(self) -> str:
        return self.name or self.card_id or "?"


@dataclass
class DarkGiftUse:
    """一次黑赐落在了谁身上（历史记录用）。"""
    minion_name: str = ""
    minion_card_id: str = ""
    minion_entity_id: Optional[int] = None


@dataclass
class AdviceReport:
    """一次评估的完整结论。可直接 JSON 化给悬浮框用。"""
    turn: int = 0
    step: str = ""
    in_recruit: bool = False
    gold: Optional[int] = None
    health: Optional[int] = None
    max_health: Optional[int] = None
    tavern_tier: Optional[int] = None
    board_size: int = 0
    hand_size: int = 0
    shop: List[ShopItem] = field(default_factory=list)
    #: 酒馆法术槽位（和 `shop` 分开：没有攻/血，费用也不一样）
    spells: List[ShopItem] = field(default_factory=list)
    triples: List[TripleCandidate] = field(default_factory=list)
    level: Optional[LevelAdvice] = None
    warnings: List[Warning] = field(default_factory=list)
    #: 「黑暗之赐」状态（当前版本 BG36 的机制，见 `DarkGift`）
    dark_gift: Optional[DarkGift] = None
    #: 当前正在等待玩家做的选一（饰品四选一 / 黑赐三选一）
    pending_choice: Optional[ChoiceAdvice] = None
    #: 已装备的饰品（小/大）
    trinkets: List[TrinketInfo] = field(default_factory=list)
    #: 本局黑赐用过几次、分别给了谁
    dark_gift_uses: List[DarkGiftUse] = field(default_factory=list)
    #: ⭐ 决策框：把上面的分析收敛成**一个**动作（悬浮框独立小窗用这个）
    decision: Optional[DecisionBox] = None
    #: 一句话总结（悬浮框顶部用）
    headline: str = ""

    def top_shop(self, n: int = 3) -> List[ShopItem]:
        return [s for s in self.shop if s.action != "跳过"][:n]


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------

class RulesEngine:
    """把 `GameState` 评估成一堆**可解释**的建议。

    用法::

        engine = RulesEngine(CardDB.load("data/cards_zhCN.json"))
        report = engine.evaluate(builder.state)
        print(report.headline)
    """

    def __init__(self, db: Optional[CardDB] = None,
                 config: Optional[RulesConfig] = None,
                 db_en: Optional[CardDB] = None,
                 trinket_stats: Optional[TrinketStats] = None,
                 hero_stats: Optional[HeroStats] = None):
        self.db = db
        #: 英文卡表（只服务 `evidence`/`choice` 题面）。缺失时静默退回中文。
        self.db_en = db_en
        #: HSReplay 饰品实证表（`trinkets.py`，由 `tools/fetch_hsreplay.py` 落盘）。
        #: 缺失时饰品四选一退回"只展示不推荐"，其余功能不受影响。
        self.trinket_stats = trinket_stats
        #: HSReplay 英雄实证表（`heroes.py`，同上工具 `--heroes` 落盘）。
        #: 缺失时英雄选择退回"只展示不推荐"，其余功能不受影响。
        self.hero_stats = hero_stats
        self.config = config or RulesConfig()
        #: card_id → 关键词元组。卡表是只读的，算一次就够（否则每张商店卡都要重扫卡文）。
        self._mech_cache: Dict[str, Tuple[str, ...]] = {}

    # -- 卡表查询（CardDB 缺失时全部安全降级） --------------------------

    def _card(self, card_id: str) -> Optional[Card]:
        if self.db is None or not card_id:
            return None
        return self.db.get(card_id)

    def _card_en(self, card_id: str) -> Optional[Card]:
        """英文卡表查询（只喂模型用，不影响任何中文展示）。"""
        if self.db_en is None or not card_id:
            return None
        return self.db_en.get(card_id)

    def _display_name(self, card_id: str, fallback: str = "") -> str:
        card = self._card(card_id)
        if card is not None and card.display_name:
            return card.display_name
        return fallback or card_id or "?"

    @staticmethod
    def _looks_like_card_id(text: str) -> bool:
        """判断一个"名字"其实是不是 CardID。

        ⚠️ 日志里 `SHOW_ENTITY`/`FULL_ENTITY` 的实体名**有时就是 CardID 本身**
        （实测商店槽位里出现过 `name == "BG36_506"`），而真名是中文（"欢悦"）。
        不做这个判据的话会在面板上直接显示 `BG36_506`。
        """
        if not text:
            return False
        if " " in text or "·" in text or "，" in text:
            return False          # 真名多半带空格/标点
        has_upper = any(c.isupper() for c in text)
        has_digit = any(c.isdigit() for c in text)
        return has_upper and has_digit and all(
            c.isascii() and (c.isalnum() or c == "_") for c in text)

    def _best_name(self, card_id: str, entity_name: str = "") -> str:
        """实体名与卡表名择优。

        优先用实体名（英雄皮肤等日志名比卡表名短，玩家更认），但**当实体名
        看起来就是 CardID 时改用卡表名**——否则会把 CardID 当卡名显示出去。

        ⚠️ 还必须排掉 `UNKNOWN ENTITY [cardType=INVALID]` / `N0NAME` 这类占位名：
        实体还没解析出来时日志给的就是它，而**卡表里其实有名字**
        （实测 `BG31_881` → 「时间管理」，日志名却是 UNKNOWN ENTITY）。
        不排掉的话决策框会输出"买 UNKNOWN ENTITY [cardType=INVALID]"。
        """
        if (entity_name and self._is_known_name(entity_name)
                and not self._looks_like_card_id(entity_name)):
            return entity_name
        return self._display_name(card_id, fallback=entity_name)

    def _mechanics(self, card_id: str) -> Tuple[str, ...]:
        """卡牌的机制关键词（`mechanics` 字段 **+** 从卡文模板抠出来的）。

        ⚠️ 别退回只读 `card.mechanics`：实测「发动」「塑造法术」都不在 mechanics
        字段里（「发动」甚至在卡表里是 `mechanics: None`），只看字段会静默漏掉
        全库 50 张「发动」卡。
        """
        if not card_id:
            return ()
        hit = self._mech_cache.get(card_id)
        if hit is not None:
            return hit
        card = self._card(card_id)
        out: List[str] = []
        taken: set = set()
        if card is not None:
            for m in card.mechanics:
                if m in _KEYWORD_MECHANICS and m not in taken:
                    taken.add(m)
                    out.append(m)
            for m in parse_text_keywords(card.text):
                # 卡文里的中文词与 mechanics 里的英文标指的是同一个机制，
                # 去重（如 AVENGE 与 复仇(2)），否则同机制进两次分。
                base = m.split("(")[0]
                equiv = _TEXT_TO_MECHANIC.get(base)
                if equiv is not None and equiv in taken:
                    continue
                if equiv is not None:
                    taken.add(equiv)
                if m not in out:
                    out.append(m)
        result = tuple(out)
        self._mech_cache[card_id] = result
        return result

    def _keyword_labels(self, card_id: str) -> List[str]:
        """给面板/CLI 用的中文关键词标签（去掉 `发动(0)` 里的费用）。

        ⚠️ 标签与 `MECHANIC_WEIGHT` 的键**不是**同一套：权重键要把费用带进字符串
        （`发动(0)` 比 `发动(2)` 值钱），标签要的是干净的中文名。
        """
        out: List[str] = []
        for m in self._mechanics(card_id):
            base = m.split("(")[0]
            zh = MECHANIC_ZH.get(base) or MECHANIC_ZH.get(m)
            if zh and zh not in out:
                out.append(zh)
        return out

    def _race(self, card_id: str) -> str:
        card = self._card(card_id)
        if card is None:
            return ""
        return card.race or (card.races[0] if card.races else "")

    # -- 主入口 ---------------------------------------------------------

    def evaluate(self, st: GameState) -> AdviceReport:
        seat = st.local_seat
        rep = AdviceReport(
            turn=st.turn,
            step=st.step,
            in_recruit=st.in_recruit_phase,
            gold=seat.gold if seat else None,
            health=seat.effective_health if seat else None,
            max_health=seat.max_health if seat else None,
            tavern_tier=seat.tech_level if seat else None,
            board_size=len(st.board),
            hand_size=len(st.hand),
        )
        cfg = self.config

        owned = self._owned_counts(st)
        rep.triples = self._find_triples(st, owned)
        rep.shop = self._score_shop(st, owned)
        rep.spells = self._score_spells(st)
        rep.level = self._advise_level(st, seat, rep.shop)
        # ⚠️ 黑暗之赐必须在 `_warnings` **之前**算：预警里要提醒"能点了，别忘拿"，
        # 而预警读的是 `rep.dark_gift`。反过来写的话这个提醒永远不出现（踩过）。
        rep.dark_gift = self._dark_gift(st)
        # ⚠️ 与 `dark_gift` 同理：`_warnings` 要提醒"有选择没做"，
        # 所以待选与饰品必须在预警之前算好。
        rep.pending_choice = self._choice_advice(st)
        rep.trinkets = self._trinkets(st)
        rep.dark_gift_uses = [
            DarkGiftUse(g.minion_name, g.minion_card_id, g.minion_entity_id)
            for g in getattr(st, "dark_gifts", [])
        ]
        # ⭐ 决策框：把上面这些分析收敛成一个动作。放在预警之前，
        # 因为预警要能引用决策（"曲线说该升了但你升不了"这类）。
        rep.decision = self._decision(st, seat, rep)
        rep.warnings = self._warnings(st, seat, rep)
        rep.headline = self._headline(rep)
        return rep

    # -- 待选（饰品四选一 / 黑赐三选一） --------------------------------

    def _choice_advice(self, st: GameState) -> Optional[ChoiceAdvice]:
        """把盘面上的 `pending_choice` 翻成带卡表信息的展示结构。

        ⚠️ 打分范围：饰品（`_score_trinket_options`）与**开局英雄选择**
        （`_score_hero_options`，HSReplay 选取率）有实证分。赐福与"卡牌效果
        触发的发现"没有可复现的样本，凭卡文猜分数只会误导，继续只做
        "列清楚是什么 + 一行特征提示"。
        普通起手换牌（MULLIGAN）直接跳过 —— 与战棋决策无关（**英雄选择在日志里
        同样报 MULLIGAN**，实测三份会话一致，靠"选项卡号里带 `_HERO`"区分）。
        """
        pc = getattr(st, "pending_choice", None)
        if pc is None or not getattr(pc, "is_local", False):
            return None
        pc_kind = getattr(pc, "kind", "")
        hero_pick = (pc_kind == "MULLIGAN"
                     and any("_HERO" in (o.card_id or "") for o in pc.options))
        if pc_kind == "MULLIGAN" and not hero_pick:
            return None

        if hero_pick:
            kind = "hero"
        elif pc.is_trinket:
            kind = "trinket_large" if pc.trinket_size == "large" else "trinket"
        elif pc.is_dark_gift:
            kind = "dark_gift"
        else:
            kind = "other"

        options: List[ChoiceItem] = []
        for o in sorted(pc.options, key=lambda x: x.index):
            card = self._card(o.card_id)
            card_en = self._card_en(o.card_id)
            item = ChoiceItem(
                index=o.index,
                card_id=o.card_id,
                # 卡表名优先于日志名：饰品/赐福的日志名是 `朝圣小鬼标签` 这种
                # 内部占位名，卡表才是玩家看到的正式名（实测差值很常见）。
                name=(card.name if card and card.name else (o.name or o.card_id)),
                tech_level=card.tech_level if card else None,
                attack=card.attack if card else None,
                health=card.health if card else None,
                text=card.text if card else "",
                kind=("hero" if kind == "hero"
                      else ("trinket" if kind.startswith("trinket") else "minion")),
                # ⚠️ 英文靠 **CardID** 取（语言无关）；用中文名反查英文卡表不可靠
                # （中文名与英文名不是直译，日志里还有第三套皮肤短名）。
                name_en=(card_en.name if card_en and card_en.name else ""),
                text_en=(self._strip_tags(card_en.text) if card_en else ""),
            )
            self._attach_gift(item, o)
            item.note = self._choice_note(item)
            options.append(item)

        src_card = self._card(getattr(pc, "source_card_id", ""))
        src_card_en = self._card_en(getattr(pc, "source_card_id", ""))
        advice = ChoiceAdvice(
            kind=kind,
            title=getattr(pc, "source_name", "") or getattr(pc, "source_card_id", ""),
            source_name=getattr(pc, "source_name", ""),
            source_text=self._strip_tags(src_card.text) if src_card else "",
            title_en=("Hero selection" if kind == "hero"
                      else (src_card_en.name if src_card_en and src_card_en.name
                            else (getattr(pc, "source_name", "")
                                  or getattr(pc, "source_card_id", "")))),
            source_text_en=(self._strip_tags(src_card_en.text) if src_card_en else ""),
            is_local=True,
            options=options,
        )
        self._score_trinket_options(advice)
        self._score_hero_options(advice)
        return advice

    def _score_trinket_options(self, advice: ChoiceAdvice) -> None:
        """给饰品四选一的选项补 HSReplay 实证分，并选出推荐项。

        ⚠️ 只对**饰品**打分：黑赐选项是随从（tier 表里没有），三连奖励这类
        卡牌效果发现也不在表里 —— 它们保持原来的"只展示不推荐"。
        ⚠️ 部分选项没数据时（新补丁刚上的饰品 HSReplay 还没统计到）仍然推荐
        "有数据的里面最好的"，但必须把缺口写进 `recommend_note` —— 新饰品
        往往很强，闷头推荐一个 D 级老饰品会误导。
        """
        stats = self.trinket_stats
        if stats is None or not advice.kind.startswith("trinket"):
            return
        missing = 0
        for item in advice.options:
            sc = stats.score(item.card_id)
            if sc is None:
                missing += 1
                continue
            item.score = sc.score
            item.score_note = sc.note
            item.score_note_en = sc.note_en
        scored = [o for o in advice.options if o.score is not None]
        if not scored:
            return
        # 同分取先出的（游戏里排在前面的选项）——顺序稳定，结果可复现。
        scored.sort(key=lambda o: (-(o.score or 0.0), o.index))
        advice.recommended = scored[0]
        if missing:
            advice.recommend_note = "%d 项无实证数据" % missing

    def _score_hero_options(self, advice: ChoiceAdvice) -> None:
        """给开局英雄选择的选项补 HSReplay 实证分，并选出推荐项。

        ⚠️ 数据是**选取率百分位**（玩家共识代理），不是平均名次 —— 免费层
        拿不到后者（见 `heroes.py` 头注）。决策要的是"这几个候选之间谁更被
        认可"的相对排序，百分位正好表达它。
        ⚠️ 同饰品：部分选项没数据时仍然推荐"有数据的里面最好的"，但把缺口
        写进 `recommend_note` —— 新英雄往往很强，闷头推荐老英雄会误导。
        """
        stats = self.hero_stats
        if stats is None or advice.kind != "hero":
            return
        missing = 0
        for item in advice.options:
            sc = stats.score(item.card_id)
            if sc is None:
                missing += 1
                continue
            item.score = sc.score
            item.score_note = sc.note
            item.score_note_en = sc.note_en
        scored = [o for o in advice.options if o.score is not None]
        if not scored:
            return
        # 同分取先出的（游戏里排在前面的选项）——顺序稳定，结果可复现。
        scored.sort(key=lambda o: (-(o.score or 0.0), o.index))
        advice.recommended = scored[0]
        if missing:
            advice.recommend_note = "%d 项无实证数据" % missing

    @staticmethod
    def _strip_tags(text: str) -> str:
        """卡文里的 `<b>…</b>` 之类标记去掉、空白压平，只留可读文字。

        ⚠️ 卡文里带换行（实测 `伦德尔标签` 正文就被折成两行），
        不压平的话面板上会多出一条空行。
        """
        import re as _re
        plain = _re.sub(r"<[^>]+>", "", text or "")
        return _re.sub(r"\s+", " ", plain).strip()

    def _attach_gift(self, item: ChoiceItem, o) -> None:
        """把选项自带的黑暗之赐补进 `ChoiceItem`（查卡表拿名字与词条）。

        ⚠️ 赐福信息是 `StateBuilder` 在选项挂上盘面时抄下来的
        （`DARK_GIFT_ENTITY` 标签每局末态会被清 0，晚查就没了），
        这里只负责把 CardID 翻成中英双份名字与卡文。
        """
        gift_id = getattr(o, "gift_card_id", "") or ""
        if not gift_id:
            return
        gift = self._card(gift_id)
        gift_en = self._card_en(gift_id)
        item.gift_card_id = gift_id
        item.gift_name = (gift.name if gift and gift.name
                          else (getattr(o, "gift_name", "") or gift_id))
        item.gift_text = gift.text if gift else ""
        item.gift_name_en = gift_en.name if gift_en and gift_en.name else ""
        item.gift_text_en = self._strip_tags(gift_en.text) if gift_en else ""

    def _choice_note(self, item: ChoiceItem) -> str:
        """从卡文提炼一行特征提示（纯展示，不含评分）。

        ⚠️ 这里**不含**黑暗之赐的赐福 —— 赐福由 `item.gift_note` 单独给出，
        因为它是"这个选项强不强"的主要依据，值得单独占一行高亮（面板与
        CLI 都单独画）；塞进 note 里会被卡文挤到看不见。
        """
        bits: List[str] = []
        # ⚠️ 星级/身材不是随从专属：饰品里也有带 techLevel 的（实测
        # `BG30_Trinket_1st` 就有 TECH_LEVEL），有就显示，不给 kind 加条件。
        if item.tech_level:
            bits.append("%d星" % item.tech_level)
        if item.attack is not None and item.health is not None:
            bits.append("%d/%d" % (item.attack, item.health))
        text = self._strip_tags(item.text)
        if text:
            # 卡文可能很长，截到 30 字够看
            bits.append(text if len(text) <= 36 else text[:35] + "…")
        return "  ".join(bits)

    def _trinkets(self, st: GameState) -> List[TrinketInfo]:
        out: List[TrinketInfo] = []
        for t in getattr(st, "trinkets", []):
            card = self._card(t.card_id)
            out.append(
                TrinketInfo(
                    size=t.size,
                    card_id=t.card_id,
                    name=(card.name if card and card.name else (t.name or t.card_id)),
                    text=self._strip_tags(card.text) if card else "",
                    tech_level=card.tech_level if card else None,
                )
            )
        return out

    # -- 黑暗之赐（BG36 机制） ------------------------------------------

    @staticmethod
    def _flag_on(tag_value: Optional[str]) -> bool:
        """日志里的布尔 tag 是字符串 `"1"`/`"0"`；`"0"` 和空串都算假。"""
        return tag_value is not None and tag_value not in ("0", "")

    @staticmethod
    def _is_known_name(name: str) -> bool:
        """实体名是不是真名字。

        ⚠️ 日志里到处是 `UNKNOWN ENTITY [cardType=INVALID]` 和 `N0NAME`（实测前者
        2 万多次），直接拿来显示会变成"黑暗之赐：UNKNOWN ENTITY [cardType=…]"。
        """
        if not name or name == "N0NAME":
            return False
        return not name.startswith("UNKNOWN ENTITY")

    def _is_dark_gift_effect(self, card_id: str) -> bool:
        """是不是「黑暗之赐」的赐福本体卡（`BG36_MidGameEffect_000tNN`）。

        ⚠️ 不写死 `startswith("BG36_MidGameEffect_")`：那只在小卡表里成立。
        `BG36_MidGameEffect_010`（`酒馆战棋黑暗之赐[DNT]`）是**机制总开关**不是某个
        赐福，必须排掉，否则它会被当成"已知赐福"显示给玩家。
        """
        if not card_id or card_id == "BG36_MidGameEffect_010":
            return False
        if card_id.startswith("BG36_MidGameEffect_"):
            return True
        card = self._card(card_id)
        return bool(card is not None and card_id.startswith("BG36")
                    and card.type not in ("MINION", "HERO", "GAME", "PLAYER"))

    def _dark_gift(self, st: GameState) -> DarkGift:
        """判断「黑暗发现」现在能不能点，以及场上哪些随从已经带赐福。

        ⚠️ 为什么不用 `BACON_DARK_GIFTS_ACTIVE`：实测它整局只在开局出现 2 次
        （是个"本局启用该机制"的开关），不是"此刻可点"。真正代表可点的是
        按钮实体上的 `BACON_DARK_GIFT_PRESSABLE_VFX`。
        """
        gift = DarkGift()
        for e in st.entities.values():
            if e.card_id == DARK_GIFT_BUTTON_CARD:
                gift.button_entity_id = e.entity_id
                if self._flag_on(e.tags.get("BACON_DARK_GIFT_PRESSABLE_VFX")):
                    gift.available = True
                continue
            # 赐福实体本身（`BG36_MidGameEffect_000tNN`）——记下名字，好告诉玩家
            # 场上那只随从吃的是哪个赐福。
            if self._is_dark_gift_effect(e.card_id):
                card = self._card(e.card_id)
                name = card.display_name if card is not None else ""
                if name and not name.endswith("[DNT]") and name not in gift.known_effects:
                    gift.known_effects.append(name)
            # 随从侧：`HAS_DARK_GIFT=1` 说明它身上挂着赐福
            if self._flag_on(e.tags.get("HAS_DARK_GIFT")):
                if e.card_id and self._is_minion_card(e.card_id):
                    # ⚠️ 刚进场时实体名可能还是空的，退回卡表名，别显示成空白
                    name = self._best_name(e.card_id, e.name)
                    if self._is_known_name(name) and name not in gift.granted:
                        gift.granted.append(name)
        return gift

    # -- 自家随从统计 ---------------------------------------------------

    def _is_minion_card(self, card_id: str) -> bool:
        """按卡表判断是不是随从。卡表缺失时退回"有攻血就当随从"。"""
        card = self._card(card_id)
        if card is not None:
            if card.type in NON_MINION_TYPES:
                return False
            return card.is_minion or card.attack is not None
        return bool(card_id) and not card_id.startswith(
            ("TB_BaconShop", "Bacon_", "BG30_Trinket", "BG36_Button", "BG_OldGod"))

    def _owned_counts(self, st: GameState) -> Dict[str, int]:
        """自家场面 + 手牌里各 `CardID` 的张数（不含商店）。"""
        counts: Dict[str, int] = {}
        for e in list(st.board) + list(st.hand):
            if e.card_id and self._is_minion_card(e.card_id):
                counts[e.card_id] = counts.get(e.card_id, 0) + 1
        return counts

    def _find_triples(self, st: GameState, owned: Dict[str, int]) -> List[TripleCandidate]:
        """找"再 1 张就三连"的候选。已有三张的不再提示（已经合成了）。"""
        shop_ids = {s.card_id for s in st.shop_minions if s.card_id}
        out: List[TripleCandidate] = []
        for card_id, n in owned.items():
            if n < 2 or n % 3 == 0:
                continue
            card = self._card(card_id)
            out.append(TripleCandidate(
                card_id=card_id,
                name=self._display_name(card_id),
                owned=n,
                in_shop=card_id in shop_ids,
                tech_level=card.tech_level if card else None,
            ))
        # 商店里能直接买的排前面
        out.sort(key=lambda t: (not t.in_shop, -(t.tech_level or 0)))
        return out

    # -- 商店打分 -------------------------------------------------------

    def _score_shop(self, st: GameState, owned: Dict[str, int]) -> List[ShopItem]:
        cfg = self.config
        items: List[ShopItem] = []
        gold = st.local_seat.gold if st.local_seat else None
        board_ids = [e.card_id for e in st.board]

        for i, slot in enumerate(st.shop_minions, 1):
            card_id = slot.card_id
            card = self._card(card_id)
            name = self._best_name(card_id, slot.name)
            score = 0.0
            reasons: List[str] = []

            # --- 基础分：身材 ---
            atk = slot.attack or 0
            hp = slot.health or 0
            body = atk + hp
            score += body * 0.5
            if body >= 10:
                reasons.append(f"身材好 {atk}/{hp}")

            # --- 星级（高星更值钱）---
            tl = slot.tech_level or (card.tech_level if card else None) or 0
            score += tl * 2.0

            # --- 三连 / 对子（最重要）---
            n = owned.get(card_id, 0)
            completes = n >= 2 and n % 3 != 0
            makes_pair = n == 1
            if completes:
                score += 25.0
                reasons.append(f"凑三连（已有 {n} 张）")
            elif makes_pair:
                score += 6.0
                reasons.append("凑对子")

            # --- 金色（自带三连收益）---
            if slot.golden:
                score += 15.0
                reasons.append("金色")

            # --- 关键词（mechanics 字段 + 卡文模板，见 `_mechanics()`）---
            keywords = self._keyword_labels(card_id)
            kw_score = 0.0
            for m in self._mechanics(card_id):
                w = MECHANIC_WEIGHT.get(m, 0.0)
                if w:
                    kw_score += w
            if kw_score:
                score += kw_score
                if keywords:
                    reasons.append("关键词：" + "/".join(keywords))

            # --- 与场面同族（走流派）---
            race = self._race(card_id)
            if race:
                same = sum(1 for cid in board_ids if self._race(cid) == race)
                if same >= 2:
                    score += min(6.0, same * 1.5)
                    reasons.append(f"与场面同族（{same} 只）")

            # --- 买得起吗 ---
            affordable = gold is None or gold >= cfg.buy_cost
            if not affordable:
                score -= 10.0
                reasons.append("金币不足")

            # --- 决策 ---
            # ⚠️ 动作**不在这里定**，等这一店的随从全部打完之后，按**店内排名**
            # 统一分派（见 `_assign_shop_actions`）。旧的绝对分数线（≥18 才"买"）
            # 经真机实测没有预测力，详见那个方法的注释。
            action = "跳过"
            if not affordable:
                action = "跳过"

            items.append(ShopItem(
                index=i,
                slot_entity_id=slot.slot_entity_id,
                card_id=card_id,
                name=name,
                tech_level=slot.tech_level or (card.tech_level if card else None),
                attack=slot.attack,
                health=slot.health,
                golden=slot.golden,
                cost=cfg.buy_cost,
                score=round(score, 1),
                action=action,
                reasons=reasons,
                completes_triple=completes,
                makes_pair=makes_pair,
                race=race,
                keywords=keywords,
            ))

        self._assign_shop_actions(items, st, cfg)
        items.sort(key=lambda it: -it.score)
        return items

    #: 店内第 1 名低于这个分就不劝买（降级成"考虑"）。
    #: 依据 `tools/calibrate_shop.py` 实测（5 份真机日志、83 个商店快照）：真人买走
    #: 「店内第 1 名」的比例在**最低的 4-7 分段仍有 91.7%**，所以地板必须压到 4 分 ——
    #: 它的作用只是拦住明显不该买的牌，不是用来判断"值不值得买"。取 6.0 会把大量
    #: 真人真会买的牌判成"考虑"（一张 1 星 2/3 的裸随从就只值 4.5 分）。
    BUY_TOP_MIN = 4.0

    def _assign_shop_actions(self, items: List["ShopItem"], st: GameState,
                             cfg) -> None:
        """按**店内排名**（而不是绝对分数线）给每个槽位定动作。

        实测依据（`tools/calibrate_shop.py`，5 份真机日志 / 83 个商店快照）：

          · **买不买 ≈ 与分数无关**。真人买走「店内第 1 名」的比例按分数段是
            4-7 分 **91.7%**、7-10 分 **100%**、10-13 分 94.4%、13-16 分 69.2%、
            16-19 分 75%、19-22 分 87.5%、22+ 分 71.4% —— 基本持平。
          · 用「店内最高分」预测"这回合会不会买"，**精确率在 0~24 分的每个阈值上
            都贴着基础率 84% 不动**，说明分数对"买不买"没有预测力。真正决定
            "这回合买不买"的是金币、场面格子和要不要升本。
          · 但**分数排序是有效的**：真人选中的那张，店内排名第 1 占 **47.1%**、
            前 2 占 **66.7%**（纯随机约 30%）；中局（5-9 回合）第 1 名命中 64.7%。

        所以：**分数只负责排序，"买不买"交给金币 / 场面 / 升本的约束**。旧逻辑用
        `score >= 18` 才说"买"，而真人会买的牌里绝大多数只有 4~13 分，于是引擎在
        真人 85% 会买的局面里一直说"跳过" —— 这正是决策框"几乎从不说买"的原因。

        另一个实测结论：**场面已满的店恰是最高分的店**（店内最高分 p50 = 18.3，
        而全场 p50 = 11.7），此时高分毫无意义 —— 所以场面满时把"买"降级为"考虑"，
        并把理由写成"场面已满"。
        """
        board_full = len(st.board) >= cfg.board_limit
        gold = st.local_seat.gold if st.local_seat else None
        ranked = sorted(items, key=lambda it: -it.score)
        for rank, it in enumerate(ranked, 1):
            affordable = gold is None or gold >= it.cost
            if not affordable:
                it.action = "跳过"
                continue
            if it.completes_triple:
                it.action = "买"
            elif rank == 1:
                it.action = "买" if it.score >= self.BUY_TOP_MIN else "考虑"
            elif rank <= 3:
                it.action = "考虑"
            else:
                it.action = "跳过"

            if rank == 1 and it.action in ("买", "考虑"):
                it.reasons.append("本店最高分")
            # 场面满了且没有三连收益 / 不是金色，就别买了
            if board_full and it.action == "买" and not it.golden:
                it.action = "考虑"
                it.reasons.append("场面已满")

    def _spell_effects(self, text: str) -> List[str]:
        """按卡文判酒馆法术的**效果**类别（可多类）。

        ⚠️ 用**卡文**而不是 `mechanics`：实测酒馆法术的 `mechanics` 大多为空，
        效果全写在正文里（例如 `BG28_810` 酒馆币的正文只有"获得1枚铸币。"）。

        ⚠️ 匹配前必须**去掉空白**：卡表正文里 `<b>` 标签被剥掉后会留下空格
        （实测「随机使酒馆中的一个随从变为 金色」），照原样匹配 `变为金色` 会漏。
        """
        if not text:
            return []
        flat = re.sub(r"\s+", "", text)
        return [label for label, pat in SPELL_EFFECT_PATTERNS
                if re.search(pat, flat)]

    def _score_spells(self, st: GameState) -> List[ShopItem]:
        """给酒馆法术打分。

        ⚠️ 法术**不在 `st.shop` 里**（它走 `TB_BaconShop_DragBuy_Spell`，
        没有攻/血、买价也不是 3），所以必须单独算一遍 —— 否则整个建议里
        看不到商店里那张 1 金法术。

        打分口径（刻意保守，法术强弱高度依赖卡文，样本不够时不硬猜）：
          · 起手 6 分（法术是"即用即走"的灵活资源）
          · 费用越高越难用（每金 −1）
          · 星级越高越强（+1.5/星）
          · 关键词照 `MECHANIC_WEIGHT` 加权
          · ⭐ **按效果分类加分**（见 `SPELL_EFFECT_PATTERNS`）：给牌 > 给钱 >
            刷新/给身材 > 给关键词；变身/异常减分
          · ⭐ **全局增益随场面放大**（"使你的随从"/"每个"），单体增益在空场减分
          · ⭐ **场面已满时加分**：法术不占格子，此时它比随从更值
        """
        cfg = self.config
        gold = st.local_seat.gold if st.local_seat else None
        board_full = len(st.board) >= cfg.board_limit
        items: List[ShopItem] = []

        for i, slot in enumerate(st.shop_spells, 1):
            # ⚠️ 槽位可能指向一个还没解析出来的实体（实测会出现
            # `UNKNOWN ENTITY [cardType=INVALID]`），这种不能进建议列表，
            # 否则决策框会输出"买 UNKNOWN ENTITY [cardType=INVALID]"。
            if not slot.card_id:
                continue
            card = self._card(slot.card_id)
            name = self._best_name(slot.card_id, slot.name)
            if not self._is_known_name(name) and card is None:
                continue
            cost = slot.cost
            if cost is None and card is not None:
                cost = card.cost
            if cost is None:
                cost = 1                      # 卡表也查不到时按最便宜算
            text = self._strip_tags(card.text) if card is not None else ""

            score = 6.0
            reasons: List[str] = []
            if cost:
                score -= cost * 1.0
            if cost >= 3:
                reasons.append(f"要 {cost} 金")

            tl = card.tech_level if card is not None else None
            if tl:
                score += tl * SPELL_TECH_LEVEL_WEIGHT

            keywords = self._keyword_labels(slot.card_id)
            kw_score = 0.0
            for m in self._mechanics(slot.card_id):
                kw_score += MECHANIC_WEIGHT.get(m, 0.0)
            if kw_score:
                score += kw_score
                if keywords:
                    reasons.append("关键词：" + "/".join(keywords))

            if board_full:
                score += 3.0
                reasons.append("场面已满（法术不占格子）")

            # ⭐ 按效果分类调分（只调法术内部顺序，不和随从横向比）
            effects = self._spell_effects(text)
            if effects:
                score += sum(SPELL_EFFECT_WEIGHT.get(e, 0.0) for e in effects)
                reasons.append("效果：" + "/".join(effects))
                if "给身材" in effects or "给关键词" in effects:
                    if _SPELL_GLOBAL_RE.search(text):
                        # 全局增益：吃几只，价值就涨多少（封顶 2 分）
                        bonus = min(0.4 * max(0, len(st.board) - 1), 2.0)
                        if bonus:
                            score += bonus
                            reasons.append("吃场面（%d 只）" % len(st.board))
                    elif _SPELL_SINGLE_RE.search(text) and not st.board:
                        score -= 1.5
                        reasons.append("场上没有随从可贴")

            affordable = gold is None or gold >= cost
            if not affordable:
                score -= 10.0
                reasons.append("金币不足")

            if not affordable:
                action = "跳过"
            elif score >= 9.0:
                action = "买"
            elif score >= 4.5:
                action = "考虑"
            else:
                action = "跳过"

            items.append(ShopItem(
                index=i,
                slot_entity_id=slot.slot_entity_id,
                card_id=slot.card_id,
                name=name,
                tech_level=tl,
                attack=None,
                health=None,
                golden=False,
                cost=cost,
                score=round(score, 1),
                action=action,
                reasons=reasons,
                keywords=keywords,
                kind="spell",
                text=text,
            ))

        items.sort(key=lambda it: -it.score)
        return items

    # -- 升本建议 -------------------------------------------------------

    def tech_up_cost(self, st: GameState) -> Tuple[Optional[int], Optional[int]]:
        """(下一档费用, 下一档等级)。优先读升本按钮的实时 `COST`。"""
        seat = st.local_seat
        if seat is None or seat.tech_level is None:
            return None, None
        next_tier = seat.tech_level + 1
        if seat.max_tech_level is not None and next_tier > seat.max_tech_level:
            return None, None
        for e in st.entities.values():
            if e.zone != "PLAY" or not e.card_id.startswith(TECHUP_BUTTON_PREFIX):
                continue
            tl = e.tech_level
            if tl == next_tier:
                cost = _cost_of(e)
                if cost is not None:
                    return cost, next_tier
        return self.config.tech_up_costs.get(next_tier), next_tier

    def reroll_cost(self, st: GameState) -> Optional[int]:
        """本回合刷新费用（0 = 有免费刷新）。"""
        for e in st.entities.values():
            if e.zone == "PLAY" and e.card_id == REROLL_BUTTON_CARD:
                cost = _cost_of(e)
                if cost is not None:
                    return cost
        return None

    def free_refreshes(self, st: GameState) -> Optional[int]:
        for e in st.entities.values():
            v = e.tags.get("BACON_FREE_REFRESH_COUNT")
            if v is not None and (e.card_id == FREE_REFRESH_ENCH or "FREE_REFRESH" in e.card_id):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
        return None

    def _advise_level(self, st: GameState, seat: Optional[Seat],
                      shop: List[ShopItem]) -> LevelAdvice:
        if seat is None:
            return LevelAdvice(None, None, None, False, 0.0, "观望",
                               ["还没有本地玩家信息"])
        cost, next_tier = self.tech_up_cost(st)
        gold = seat.gold
        cur = seat.tech_level
        adv = LevelAdvice(current_tier=cur, next_tier=next_tier, cost=cost,
                          affordable=bool(cost is not None and gold is not None
                                          and gold >= cost),
                          score=0.0, action="观望")
        if cost is None or next_tier is None:
            adv.action = "不升"
            adv.reasons.append("已到最高酒馆等级"
                               if cur is not None else "等级未知")
            return adv

        # --- 打分 ---
        score = 0.0
        # 早升价值高：低档位时升本收益大
        if cur is not None and cur <= 3:
            score += 6.0
            adv.reasons.append(f"{cur} 本早升收益大")
        elif cur is not None and cur >= 5:
            score -= 4.0
            adv.reasons.append(f"{cur} 本升本收益递减")
        # 买不起**不是**"不该升"，只是"这回合升不了"。
        # ⚠️ 早先这里给 -12，分值大到把「低本早升收益大 +6」整个压过去，
        # 于是"四费升二本"这种最标准的打法会被判成"不升"（真机踩过，见 §11.11）。
        if not adv.affordable:
            score -= 4.0
            adv.reasons.append(f"金币不足（要 {cost}，只有 {gold}）")
        # 商店里有三连就别升本了
        if any(s.completes_triple for s in shop):
            score -= 15.0
            adv.reasons.append("商店有三连可拿，先买")
        # 场面弱的时候升本会挨打。⚠️ 低档位（<=3 本）例外：那时场面本来就小，
        # 前几回合按曲线升本才是标准打法，不能因为"只有 2 只"就劝人别升。
        if st.board and len(st.board) >= 5:
            score += 3.0
        elif cur is not None and cur <= 3:
            score += 1.0
            adv.reasons.append("低本阶段场面自然偏小")
        else:
            score -= 2.0
            adv.reasons.append("场面偏薄")

        adv.score = round(score, 1)
        if not adv.affordable:
            # ⚠️ 不叫「攒钱」——酒馆战棋金币**不跨回合**（已实测 RESOURCES 每回合
            # 重置），这个动作只表示"这一回合付不起升本费"。
            adv.action = "升不起"
        elif score >= 5.0:
            adv.action = "升本"
        elif score >= 0.0:
            adv.action = "观望"
        else:
            adv.action = "不升"
        return adv

    # -- 决策框（把上面这些分析收敛成**一个**动作） ---------------------

    @staticmethod
    def _curve_tier(turn: int) -> Optional[int]:
        """按 `LEVEL_CURVE`，这个回合**应该**已经到的酒馆等级。"""
        tier: Optional[int] = None
        for t, level in LEVEL_CURVE:
            if turn >= t:
                tier = level
            else:
                break
        return tier

    def _level_guard(self, st: GameState, seat: Seat,
                     lv: LevelAdvice) -> Optional[str]:
        """返回一个「现在不该升本」的理由；没有就返回 None。

        ⚠️ **只在 3 本及以上才拦**：低本阶段场面本来就小，
        「四费升二本」的时候场上常常一只随从都没有 —— 那时按曲线升才是标准打法。
        这条守卫对应的是"别在不该升的时候大代价贸然升本"：
        把钱一次性砸进升本、升完一分不剩、场面又薄，接下来几回合既没战力也没钱补。
        """
        cur = lv.current_tier
        if cur is None or cur < 3:
            return None
        gold, cost = seat.gold, lv.cost
        if gold is None or cost is None:
            return None
        board = len(st.board)
        if cost >= max(0, gold - 2) and board < 5:
            return (f"升完只剩 {max(0, gold - cost)} 金、场上只有 {board} 只，"
                    f"先补场面")
        return None

    def _decision(self, st: GameState, seat: Optional[Seat],
                  rep: AdviceReport) -> DecisionBox:
        """把 `rep` 里的分析收敛成**一个**动作。

        这里只做一件事：给「黑暗发现可点」降级。它的判据是按钮上的
        `BACON_DARK_GIFT_PRESSABLE_VFX`，而实测这个 flag **会连续亮 4 个回合**
        （T5 `MAIN_START_TRIGGERS` 亮 → T9 `MAIN_ACTION` 灭），如果把它放在
        买牌/升本前面，决策框会连续 4 回合只说"先选牌"，把真正的建议全挡住。
        所以它只在"本来没事干"时才升级成主动作，否则退成理由行的一句话。
        """
        box = self._decision_core(st, seat, rep)
        gift = rep.dark_gift
        if gift is None or not gift.available:
            return box
        if box.action == "观望":
            box.action, box.target, box.cost = "选择", "黑暗发现", 0
            box.trigger, box.urgency = "dark_gift", "now"
            box.reason = "「黑暗发现」能点了，免费三选一"
            return box
        # 已经弹出的模态选一比它更急，别再往上堆字
        if box.trigger != "pending_choice":
            extra = "（黑暗发现可点）"
            box.reason = (box.reason + extra) if box.reason else extra.strip("（）")
        return box

    def _decision_core(self, st: GameState, seat: Optional[Seat],
                       rep: AdviceReport) -> DecisionBox:
        box = DecisionBox(gold=rep.gold, curve_tier=self._curve_tier(rep.turn))
        # ⚠️ 英雄选择要先于"座位/阶段"两个兜底处理：它发生在 `STEP=BEGIN_MULLIGAN`
        # （实测）——此时座位还没建、也不在招募期，但它是卡住游戏的模态选择。
        pc0 = rep.pending_choice
        hero_pick = pc0 is not None and pc0.kind == "hero"
        if seat is None and not hero_pick:
            box.action, box.trigger, box.urgency = "观望", "idle", "wait"
            box.reason = "还没有本地玩家信息"
            return box
        if not rep.in_recruit and not hero_pick:
            box.action, box.trigger, box.urgency = "观望", "idle", "wait"
            box.reason = "战斗阶段，等结算"
            return box

        # 0. 已经弹出的选一永远排第一 —— 模态框卡着回合，不点就没法继续
        if rep.pending_choice is not None:
            pc = rep.pending_choice
            box.action, box.target, box.cost = "选择", pc.display_title, 0
            box.trigger, box.urgency = "pending_choice", "now"
            # 有实证推荐（英雄选择 / 饰品四选一）就说清楚推荐哪个；
            # 没有就退回通用文案（英雄选择的"免费收益"说不通，单独给一句）。
            box.reason = pc.recommend_text or (
                "选好英雄再开局" if pc.kind == "hero" else "免费收益，先点开挑一个")
            return box

        gold = rep.gold or 0
        cur = seat.tech_level
        target = box.curve_tier
        lv = rep.level
        off_curve = ""

        # 1. 曲线说该升了 —— 这是默认答案，只有守卫反对才偏离
        if (target is not None and cur is not None and cur < target
                and lv is not None and lv.next_tier is not None
                and lv.cost is not None):
            if gold >= lv.cost:
                blocked = self._level_guard(st, seat, lv)
                if blocked is None:
                    box.action = "升本"
                    box.target = f"{lv.next_tier}本"
                    box.cost = lv.cost
                    box.trigger, box.urgency = "curve", "soon"
                    if lv.next_tier < target:
                        box.reason = (f"曲线：{rep.turn} 回合目标 {target} 本，"
                                      f"先上 {lv.next_tier} 本")
                    else:
                        box.reason = f"曲线：{rep.turn} 回合该到 {target} 本"
                    return box
                off_curve = blocked
                box.off_curve = True
            else:
                # ⚠️ 这里**不能**说「攒钱」——酒馆战棋的金币**不跨回合保留**
                # （实测 `RESOURCES` 每回合重置成回合值：3/4/5/…/10，不是累加），
                # 所以"这回合不花、下回合升"根本不成立。正确做法是这回合把金币
                # 花在能提升的地方（买牌或刷新），升级等攒够的那一回合再说。
                off_curve = f"升本差 {lv.cost - gold} 金"
                box.off_curve = True
        elif target is not None and cur is not None and cur > target:
            # 已经超前，不用急着升
            box.off_curve = True

        # 2. 能凑三连就先凑（三连的收益比单张买牌大得多）
        triple = next((s for s in rep.shop
                       if s.completes_triple and s.cost <= gold), None)
        if triple is not None:
            box.action, box.target, box.cost = "买", triple.name, triple.cost
            box.trigger, box.urgency = "triple", "soon"
            box.reason = off_curve or "凑三连"
            return box

        # 3. 规则明确说「买」的
        best = next((s for s in rep.shop if s.action == "买" and s.cost <= gold), None)
        if best is not None:
            box.action, box.target, box.cost = "买", best.name, best.cost
            box.trigger, box.urgency = "shop", "soon"
            box.reason = off_curve or (best.reasons[0] if best.reasons else "")
            return box

        # 4. 场面太薄 → 就算只是「考虑」也先买一只撑着。
        #    ⚠️ 早期（场上不到 3 只）不看血线：这时候没场面本身就是最大风险，
        #    买身材远比买法术重要 —— 否则决策框会在回合 1 让你买法术。
        thin = rep.board_size < 3 or (
            rep.board_size < 5 and rep.health is not None
            and rep.health <= self.config.low_health)
        if thin:
            cand = next((s for s in rep.shop
                         if s.action == "考虑" and s.cost <= gold), None)
            if cand is None and rep.board_size < 3:
                # 场上太空时身材比什么都重要 —— 哪怕规则给了「跳过」也先占住
                # 格子。不这样兜底的话，回合 1 空场会去**买法术**（法术打分和
                # 随从打分不是同一把尺子，不能直接比大小，得按类别排序）。
                cand = next((s for s in rep.shop if s.cost <= gold), None)
            if cand is not None:
                box.action, box.target, box.cost = "买", cand.name, cand.cost
                box.trigger, box.urgency = "thin_board", "soon"
                box.reason = off_curve or ("场面太空，先买身材"
                                           if rep.board_size < 3 else "场面偏薄，先撑住")
                return box

        # 4b. 酒馆法术：**不占格子**，而且这是最后几个能花钱的地方之一
        #     （金币不跨回合，什么都不做等于白扔）。走到这里说明没有更值得买的
        #     随从、也没到该升本的时候 —— 那就把法术拿了。
        spell = next((s for s in rep.spells if s.action == "买" and s.cost <= gold),
                     None)
        if spell is None:
            spell = next((s for s in rep.spells
                          if s.action == "考虑" and s.cost <= gold), None)
        if spell is not None:
            box.action, box.target, box.cost = "买", spell.name, spell.cost
            box.trigger, box.urgency = "spell", "soon"
            box.reason = off_curve or (spell.reasons[0] if spell.reasons
                                       else "没有更值得买的，法术不占格子")
            return box

        # 5. 有免费刷新就别浪费
        if gold > 0 and (self.free_refreshes(st) or 0) > 0:
            box.action, box.cost = "刷新", 0
            box.trigger, box.urgency = "free_refresh", "soon"
            box.reason = off_curve or "有免费刷新，找更好的牌"
            return box

        # 6. 还剩金币就别留着 —— 金币不跨回合（见上面那条注释），
        #    花 1 金刷新找更好的，比什么都不做强。
        if gold > 0:
            box.action, box.cost = "刷新", self.reroll_cost(st)
            box.trigger, box.urgency = "spend_gold", "soon"
            n = self.config.reroll_cost
            box.reason = off_curve or f"还剩 {gold} 金（不会留到下一回合），刷新花 {n} 金"
            return box

        box.action, box.trigger, box.urgency = "观望", "idle", "wait"
        box.reason = off_curve or "没有值得花的钱"
        return box

    # -- 血线 / 其他预警 ------------------------------------------------

    def _warnings(self, st: GameState, seat: Optional[Seat],
                  rep: AdviceReport) -> List[Warning]:
        out: List[Warning] = []
        cfg = self.config
        # 待选（饰品四选一 / 黑赐三选一）：放在最前面 —— 不做决定就卡住回合，
        # 比血线更紧急。
        choice = rep.pending_choice
        if choice is not None:
            if choice.recommended is not None:
                out.append(Warning(
                    "info",
                    f"{choice.display_title}还没选——推荐 {choice.recommended.label}"))
            else:
                out.append(Warning("info", f"{choice.display_title}还没选——点开挑一个"))
        # 黑暗之赐：能点就提醒——这是个**免费收益**，漏点就是白亏。
        gift = rep.dark_gift
        if gift is not None and gift.available:
            out.append(Warning("info", "「黑暗发现」可以点了——免费三选一，别忘拿"))
        elif gift is not None and gift.granted:
            out.append(Warning("info", "黑暗之赐：" + "、".join(gift.granted[:3])))
        hp = rep.health
        if hp is not None:
            cap = self._combat_damage_cap(st) or cfg.combat_damage_cap
            if hp <= cfg.critical_health:
                out.append(Warning("danger", f"血量 {hp}，濒死——优先补场面"))
            elif hp <= cfg.low_health:
                extra = f"（下场最多挨 {cap}）" if cap else ""
                out.append(Warning("warn", f"血量 {hp}，进入危险区{extra}"))
        if seat is not None and seat.died_last_combat:
            out.append(Warning("warn", "上回合战败，注意血量"))
        if len(st.board) < 3 and st.turn >= 5:
            out.append(Warning("warn", f"场面只有 {len(st.board)} 只，偏薄"))
        if len(st.hand) >= cfg.hand_limit:
            out.append(Warning("info", f"手牌 {len(st.hand)} 张，接近上限"))
        for t in rep.triples[:2]:
            if t.in_shop:
                out.append(Warning("info", f"{t.name} 在售，可直接凑三连"))
        free = self.free_refreshes(st)
        if free:
            out.append(Warning("info", f"有 {free} 次免费刷新"))
        return out

    def _combat_damage_cap(self, st: GameState) -> Optional[int]:
        for e in st.entities.values():
            v = e.tags.get("BACON_COMBAT_DAMAGE_CAP")
            if v is not None and v != "":
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
        return None

    # -- 一句话总结 -----------------------------------------------------

    def _headline(self, rep: AdviceReport) -> str:
        parts: List[str] = []
        if rep.in_recruit:
            parts.append(f"回合 {rep.turn} 招募")
        else:
            parts.append(f"回合 {rep.turn} 战斗")
        if rep.gold is not None:
            parts.append(f"{rep.gold} 金")
        if rep.health is not None:
            parts.append(f"{rep.health} 血")
        # ⚠️ 标题**不再**重复动作：动作由决策框（独立小窗）单独说。
        # 早先只有一个框时，这里必须和决策框说同一件事，否则同一屏里标题说
        # "建议买 X"、升本栏说"升本"，两个结论互相打架；现在两块分工明确
        # ——决策框说"做什么"，这里只说"现在什么情况"。
        return " · ".join(parts)


def _cost_of(e: Entity) -> Optional[int]:
    """读实体的 `COST`。⚠️ 战棋按钮的费用有时写在 `COST`，缺省时打 `BACON_COST`。"""
    for key in ("COST", "BACON_COST"):
        v = e.tags.get(key)
        if v is None or v == "":
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def evaluate_state(st: GameState, db: Optional[CardDB] = None,
                   config: Optional[RulesConfig] = None,
                   db_en: Optional[CardDB] = None,
                   trinket_stats: Optional[TrinketStats] = None,
                   hero_stats: Optional[HeroStats] = None) -> AdviceReport:
    """便捷函数：`RulesEngine(db, config, db_en, trinket_stats, hero_stats).evaluate(st)`。"""
    return RulesEngine(db, config, db_en, trinket_stats, hero_stats).evaluate(st)
