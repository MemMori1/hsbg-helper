"""卡牌数据层（P2）。

职责：把 `CardID` 翻成中文名、种族、星级、身材、关键字，供展示层与规则引擎用。

数据源（已实测，见 `docs/需求分析.md` FR-3.2）：

* ❌ 暴雪官方 API **零战棋卡**（`hearthstone.blizzard.com/en-us/api/cards?set=battlegrounds`
  返回 `{"cards":[],"cardCount":0}`；`us.api.blizzard.com/hearthstone/cards` 已 404）
* ✅ HearthstoneJSON `https://api.hearthstonejson.com/v1/latest/zhCN/cards.json`
  （10.1 MB / 36,390 张，含中文名，其中 `set == "BATTLEGROUNDS"` 5,787 张）
* ✅ 游戏内日志自举（P0 实测一份 92 MB 日志可抠出 496 条 `CardID → 中文名`）

本模块把 HearthstoneJSON 的 10 MB 全量**裁成只含战棋卡的紧凑缓存**
（`data/cards_zhCN.json`，约 300 KB），之后完全离线可用。

零第三方依赖：只用标准库 `json` / `urllib.request` / `pathlib`。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

__all__ = [
    "CARD_DB_URL",
    "CARD_DB_VERSION",
    "BATTLEGROUNDS_SET",
    "PACKAGE_DATA_DIR",
    "DEFAULT_CARD_DB_PATH",
    "DEFAULT_CARD_DB_EN_PATH",
    "Card",
    "CardDB",
    "CardResolver",
    "build_cache",
    "download_cards_json",
    "slugify_card_id",
]

#: 官方数据的社区镜像（暴雪官方 API 无战棋卡）
CARD_DB_URL = "https://api.hearthstonejson.com/v1/latest/zhCN/cards.json"
#: 英文卡表（同一份数据的 enUS 版本）。**只用于喂 JevK5 的英文题面**（ADR-4）。
#: ⚠️ 必须靠 CardID 关联，不能拿中文名去反查英文名 —— 人名/皮肤名不是直译。
CARD_DB_EN_URL = "https://api.hearthstonejson.com/v1/latest/enUS/cards.json"
CARD_DB_VERSION = 1
BATTLEGROUNDS_SET = "BATTLEGROUNDS"

_HERE = Path(__file__).resolve()
PACKAGE_DATA_DIR = _HERE.parent.parent.parent / "data"
DEFAULT_CARD_DB_PATH = PACKAGE_DATA_DIR / "cards_zhCN.json"
DEFAULT_CARD_DB_EN_PATH = PACKAGE_DATA_DIR / "cards_enUS.json"

#: 缓存里保留的字段（其余全丢，把 10 MB 压到 ~300 KB）
_KEEP_FIELDS = (
    "id",
    "name",
    "type",
    "techLevel",
    "attack",
    "health",
    "cost",
    "race",
    "races",
    "mechanics",
    "text",
    "isBattlegroundsPoolMinion",
)


def slugify_card_id(card_id: str) -> str:
    """把含有 `[` `]` 空格之类的 CardID 规范化成文件系统友好的形式。

    实测出现的形态：`TB_BaconShop_HP_038e`（正常）、
    `标记交换玩家附加效果[DNT]`（中文 + 方括号，作为 CardID 出现时会炸文件名）。

    ⚠️ 必须按 **Unicode 归类**替换，不能用 `[^0-9A-Za-z_]`：后者会把中文整体删掉，
    `标记交换玩家附加效果[DNT]` 会变成 `DNT`（实测踩过）。
    """
    s = re.sub(r"[^\w]+", "_", card_id or "", flags=re.UNICODE)
    s = re.sub(r"_{2,}", "_", s).strip("_")
    return s or "UNKNOWN"
    return slug or "UNKNOWN"


@dataclass(frozen=True)
class Card:
    """一张战棋卡（或英雄、技能、饰品）。"""

    card_id: str
    name: str = ""
    type: str = ""
    tech_level: Optional[int] = None
    attack: Optional[int] = None
    health: Optional[int] = None
    cost: Optional[int] = None
    race: str = ""
    races: tuple = ()
    mechanics: tuple = ()
    text: str = ""
    pool_minion: bool = False

    @property
    def is_minion(self) -> bool:
        return self.type == "MINION"

    @property
    def is_hero(self) -> bool:
        return self.type == "HERO"

    @property
    def display_name(self) -> str:
        """优先中文名，没有就退回 CardID（展示层不要出现空串）。"""
        return self.name or self.card_id

    @property
    def tech_level_text(self) -> str:
        return f"{self.tech_level}星" if self.tech_level else ""

    def describe(self) -> str:
        """一行的紧凑描述，给 CLI / 悬浮框用。"""
        bits = [self.display_name]
        if self.is_minion:
            if self.tech_level:
                bits.append(f"{self.tech_level}星")
            if self.attack is not None and self.health is not None:
                bits.append(f"{self.attack}/{self.health}")
            if self.race:
                bits.append(self.race)
        return " ".join(bits)

    def to_dict(self) -> dict:
        out = {"id": self.card_id, "name": self.name, "type": self.type}
        if self.tech_level is not None:
            out["techLevel"] = self.tech_level
        if self.attack is not None:
            out["attack"] = self.attack
        if self.health is not None:
            out["health"] = self.health
        if self.cost is not None:
            out["cost"] = self.cost
        if self.race:
            out["race"] = self.race
        if self.races:
            out["races"] = list(self.races)
        if self.mechanics:
            out["mechanics"] = list(self.mechanics)
        if self.text:
            out["text"] = self.text
        if self.pool_minion:
            out["isBattlegroundsPoolMinion"] = True
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Card":
        return cls(
            card_id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            type=str(d.get("type", "")),
            tech_level=_opt_int(d.get("techLevel")),
            attack=_opt_int(d.get("attack")),
            health=_opt_int(d.get("health")),
            cost=_opt_int(d.get("cost")),
            race=str(d.get("race", "")),
            races=tuple(d.get("races") or ()),
            mechanics=tuple(d.get("mechanics") or ()),
            text=str(d.get("text", "")),
            pool_minion=bool(d.get("isBattlegroundsPoolMinion")),
        )


def _opt_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def build_cache(raw: Iterable[dict], version: int = CARD_DB_VERSION) -> dict:
    """把 HearthstoneJSON 的全量列表裁成只含战棋卡的紧凑缓存。"""
    cards: Dict[str, dict] = {}
    for c in raw:
        if not isinstance(c, dict):
            continue
        if c.get("set") != BATTLEGROUNDS_SET:
            continue
        cid = c.get("id")
        if not cid:
            continue
        compact = {k: c[k] for k in _KEEP_FIELDS if k in c and c[k] not in (None, "", [], ())}
        compact["id"] = cid
        cards[cid] = compact
    return {
        "version": version,
        "source": CARD_DB_URL,
        "built_at": int(time.time()),
        "count": len(cards),
        "cards": cards,
    }


def download_cards_json(url: str = CARD_DB_URL, timeout: float = 180.0) -> List[dict]:
    """下载 HearthstoneJSON 全量 zhCN 卡表（10.1 MB）。

    ⚠️ 只走标准库 `urllib`，本机 Python 3.9.6 自带；`requests` 虽然装了但项目
    坚持零第三方依赖。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "hsbg-helper/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
    return json.loads(payload.decode("utf-8"))


def save_cache(cache: dict, path: Path = DEFAULT_CARD_DB_PATH) -> Path:
    """原子写入缓存（先写临时文件再 `replace`，避免半截文件）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    return path


def load_raw_cache(path: Path = DEFAULT_CARD_DB_PATH) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def refresh_card_db(path: Path = DEFAULT_CARD_DB_PATH, url: str = CARD_DB_URL) -> Path:
    """下载 + 裁剪 + 落盘，返回缓存路径。"""
    raw = download_cards_json(url)
    return save_cache(build_cache(raw), path)


class CardDB:
    """卡牌查询库。构造后完全离线。"""

    def __init__(self, path: Optional[Path] = None, cards: Optional[Dict[str, Card]] = None,
                 source_path: Optional[Path] = None):
        self.path = Path(path) if path is not None else DEFAULT_CARD_DB_PATH
        self.source_path = source_path
        self._cards: Dict[str, Card] = dict(cards or {})
        self._by_name: Dict[str, Card] = {}
        self._reindex()

    # -- 构造 ----------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CardDB":
        """从缓存加载。缓存不存在时抛 `FileNotFoundError`（不静默联网）。"""
        p = Path(path) if path is not None else DEFAULT_CARD_DB_PATH
        data = load_raw_cache(p)
        cards = {cid: Card.from_dict(d) for cid, d in (data.get("cards") or {}).items()}
        return cls(path=p, cards=cards)

    @classmethod
    def load_or_empty(cls, path: Optional[Path] = None) -> "CardDB":
        """加载失败就返回空库（展示层降级为只显示 CardID，不崩）。"""
        try:
            return cls.load(path)
        except (FileNotFoundError, ValueError, OSError):
            return cls(path=path)

    def _reindex(self) -> None:
        self._by_name = {}
        for c in self._cards.values():
            if c.name:
                self._by_name.setdefault(c.name, c)

    # -- 查询 ----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._cards)

    def __contains__(self, card_id: object) -> bool:
        return card_id in self._cards

    def get(self, card_id: Optional[str]) -> Optional[Card]:
        if not card_id:
            return None
        return self._cards.get(card_id)

    def by_name(self, name: str) -> Optional[Card]:
        return self._by_name.get(name or "")

    def name_of(self, card_id: Optional[str]) -> str:
        """中文名；查不到就原样返回 CardID（**不返回空串**）。"""
        c = self.get(card_id)
        if c is not None and c.name:
            return c.name
        return card_id or ""

    def minions(self) -> List[Card]:
        return [c for c in self._cards.values() if c.is_minion]

    def pool_minions(self) -> List[Card]:
        """当期随从池（`isBattlegroundsPoolMinion`）。"""
        return [c for c in self._cards.values() if c.is_minion and c.pool_minion]

    def by_tech_level(self, tech_level: int, pool_only: bool = True) -> List[Card]:
        src = self.pool_minions() if pool_only else self.minions()
        return [c for c in src if c.tech_level == tech_level]

    def by_race(self, race: str) -> List[Card]:
        """按 `BACON_SUBSET_*` 那一套种族名过滤（日志与卡表取值一一对应）。

        注意 `race == "ALL"` 的随从（如融合怪系）对任何种族都算，本方法会把
        它们**一并**带上。
        """
        wanted = (race or "").upper()
        out = []
        for c in self.pool_minions():
            races = {r.upper() for r in (c.races or ((c.race,) if c.race else ()))}
            if wanted in races or "ALL" in races:
                out.append(c)
        return out

    def races(self) -> List[str]:
        seen = set()
        for c in self.pool_minions():
            for r in (c.races or ((c.race,) if c.race else ())):
                seen.add(r)
        return sorted(seen)

    def tech_levels(self) -> List[int]:
        return sorted({c.tech_level for c in self.pool_minions() if c.tech_level})

    def stats(self) -> dict:
        pool = self.pool_minions()
        return {
            "total": len(self._cards),
            "minions": len(self.minions()),
            "pool_minions": len(pool),
            "races": len(self.races()),
            "tech_levels": self.tech_levels(),
        }

    # -- 自举校验 ------------------------------------------------------
    def verify_against_log_names(self, pairs: Dict[str, str]) -> dict:
        """用日志自举出的 `CardID → 中文名` 反向校验卡表（P0 的 496 条）。

        返回 `{ok, mismatch, missing, checked, samples}`。任何一条对不上都说明
        卡表版本与游戏版本不同步，值得报警而不是静默。
        """
        ok = mismatch = 0
        missing = []
        samples = []
        for cid, log_name in (pairs or {}).items():
            c = self.get(cid)
            if c is None:
                missing.append(cid)
                continue
            if c.name == log_name:
                ok += 1
            else:
                mismatch += 1
                if len(samples) < 10:
                    samples.append({"card_id": cid, "log": log_name, "db": c.name})
        return {
            "checked": len(pairs or {}),
            "ok": ok,
            "mismatch": mismatch,
            "missing": len(missing),
            "missing_samples": missing[:10],
            "samples": samples,
        }


class CardResolver:
    """显示名解析：**日志自举短名 > 卡表名 > CardID 本身**。

    为什么不能只用卡表：日志里出现的是**对局内短名**，实测两份英雄皮肤对不上
    （日志 `洛卡拉` / 卡表 `奥术战士洛卡拉`；日志 `阮大师` / 卡表 `神秘导师阮大师`）。
    展示层要的是游戏内文本，所以日志自举的名字优先。
    """

    def __init__(self, db: Optional[CardDB] = None,
                 log_names: Optional[Dict[str, str]] = None,
                 db_en: Optional[CardDB] = None):
        self.db = db if db is not None else CardDB()
        self.log_names: Dict[str, str] = dict(log_names or {})
        #: 英文卡表（喂 JevK5 用；缺失时模型侧退回中文名）
        self.db_en: CardDB = db_en if db_en is not None else CardDB()

    def __len__(self) -> int:
        return max(len(self.db), len(self.log_names))

    def card(self, card_id: Optional[str]) -> Optional[Card]:
        return self.db.get(card_id)

    def name_of(self, card_id: Optional[str]) -> str:
        if not card_id:
            return ""
        nm = self.log_names.get(card_id)
        if nm:
            return nm
        return self.db.name_of(card_id)

    def name_en_of(self, card_id: Optional[str]) -> str:
        """英文卡表名；查不到退回 `name_of`（中文名或 CardID）。

        ⚠️ 必须靠 CardID 查，不能拿中文名反查英文名 —— 人名/皮肤名不是直译
        （实测 `洛卡拉` ↔ `Lokholar`、`雷诺·杰克逊` ↔ `Reno Jackson`）。
        """
        c = self.db_en.get(card_id) if (self.db_en is not None and card_id) else None
        if c is not None and c.name:
            return c.name
        return self.name_of(card_id)

    def describe(self, card_id: Optional[str]) -> str:
        c = self.db.get(card_id)
        nm = self.name_of(card_id)
        if c is None or not c.is_minion:
            return nm
        bits = [nm]
        if c.tech_level:
            bits.append(f"{c.tech_level}星")
        if c.attack is not None and c.health is not None:
            bits.append(f"{c.attack}/{c.health}")
        if c.race:
            bits.append(c.race)
        return " ".join(bits)

    @classmethod
    def load(cls, db_path: Optional[Path] = None,
             names_path: Optional[Path] = None,
             db_en_path: Optional[Path] = None) -> "CardResolver":
        """三个文件都缺也能构造（返回空库 + 空覆盖表，展示层退化为 CardID）。

        `db_en_path` 是英文卡表，**只服务喂 JevK5 的英文题面**；缺失时模型侧
        退回中文名，功能不中断。
        """
        en = CardDB.load_or_empty(
            db_en_path if db_en_path is not None else DEFAULT_CARD_DB_EN_PATH)
        return cls(db=CardDB.load_or_empty(db_path),
                   log_names=_safe_load_names(names_path),
                   db_en=en)

    @classmethod
    def bundled(cls) -> "CardResolver":
        """加载项目自带缓存；缺失时返回空解析器而不是抛异常。"""
        return cls.load()


def _safe_load_names(path: Optional[Path]) -> Dict[str, str]:
    # 延迟导入避免与 `names.py` 循环依赖
    from .names import load_names
    return load_names() if path is None else load_names(path)

