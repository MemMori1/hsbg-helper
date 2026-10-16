#!/usr/bin/env python3
"""HSReplay 英雄实证数据：读取 + 打分（S1 英雄选择）。

数据由 `tools/fetch_hsreplay.py --heroes` 从 HSReplay 公开端点拉取，落盘
`data/heroes_hsreplay.json`（约 116 位英雄 × 3 个分段，见 fetch 工具头注）。

**为什么按选取率打分**：HSReplay 的完整英雄统计（`tier_v2` + 平均名次
`adjusted_avg_final_placement`）是 Tier7 订阅内容，免登录拿不到；免费层
（`/heroes/free/`）只有 **选取率 + 护甲**。选取率 = 被 offer 时被选走的百分比，
是玩家群体对英雄强度的**共识代理**——实测三个分段（ALL / TOP 50% / TOP 1%）
的排序高度一致，同一补丁内稳定可比。

打分公式（刻意简单、可解释）::

    score = 10 × 选取率在全表里的百分位

- 10 = 当前补丁最被认可的英雄，0 = 最不被认可；只用它在**同一局的几个候选
  之间**排序（绝对分数不跨补丁比较）。
- 护甲**不参与打分**：它是暴雪的平衡杠杆（弱英雄补甲 —— 实测选取率 8% 的
  英雄有 14 甲），高低不单调对应强弱，只放进展示行供人参考。

⚠️ 查不到返回 `None`（新补丁刚上的英雄 HSReplay 还没统计到），调用方必须按
"没有数据"降级 —— 绝不能把它当 0 分（那会让新英雄看起来比最冷门的还差）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .cards import PACKAGE_DATA_DIR

__all__ = [
    "DEFAULT_HERO_STATS_PATH",
    "SCORE_SCALE",
    "HeroStat",
    "HeroScore",
    "HeroStats",
    "load_or_none",
]

DEFAULT_HERO_STATS_PATH = PACKAGE_DATA_DIR / "heroes_hsreplay.json"

#: 百分位映射到 0–10 分的满分刻度。
SCORE_SCALE = 10.0


@dataclass(frozen=True)
class HeroStat:
    """一位英雄的实证统计。"""

    card_id: str
    name: str = ""
    #: 被 offer 时被选走的百分比（0–100）。
    pick_rate: Optional[float] = None
    #: 起始护甲（平衡杠杆，展示用，不参与打分）。
    armor: Optional[int] = None


@dataclass(frozen=True)
class HeroScore:
    """打分结果：一个分数 + 组成它的原始统计（展示层按需取）。"""

    score: float
    pick_rate: Optional[float] = None
    armor: Optional[int] = None
    #: 选取率的百分位（0–1），score = percentile × 10，冗余存放便于展示/调试。
    percentile: Optional[float] = None

    @property
    def note(self) -> str:
        """中文一行摘要（展示用）：`选取率78% · 护甲6`。"""
        bits: List[str] = []
        if self.pick_rate is not None:
            bits.append("选取率%.0f%%" % self.pick_rate)
        if self.armor is not None:
            bits.append("护甲%d" % self.armor)
        return " · ".join(bits)

    @property
    def note_en(self) -> str:
        """英文一行摘要（喂模型用，ADR-4：题面必须全英文）。"""
        bits: List[str] = []
        if self.pick_rate is not None:
            bits.append("picked %.0f%%" % self.pick_rate)
        if self.armor is not None:
            bits.append("armor %d" % self.armor)
        return ", ".join(bits)


def _row_to_stat(row: Dict[str, Any]) -> HeroStat:
    def num(key: str) -> Optional[float]:
        v = row.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    armor_v = num("armor")
    return HeroStat(
        card_id=str(row.get("card_id") or ""),
        name=str(row.get("name") or ""),
        pick_rate=num("pick_rate"),
        armor=int(armor_v) if armor_v is not None else None,
    )


class HeroStats:
    """`card_id → HeroStat` 查询表 + 百分位打分。

    用法::

        stats = HeroStats.load()                  # data/heroes_hsreplay.json
        sc = stats.score("BG22_HERO_004")
        sc.score, sc.note                         # 4.1, "选取率35% · 护甲10"

    ⚠️ 查不到返回 `None`（新英雄还没统计到）；调用方按"没有数据"降级。
    """

    def __init__(self, rows: Dict[str, HeroStat],
                 bracket: str = "ALL", fetched_at: str = "") -> None:
        self._rows = rows
        #: 全表选取率升序表 —— 百分位打分用（同表内相对位置）。
        self._rates: List[float] = sorted(
            s.pick_rate for s in rows.values() if s.pick_rate is not None)
        self.bracket = bracket
        self.fetched_at = fetched_at

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None,
             bracket: str = "ALL") -> "HeroStats":
        p = Path(path) if path is not None else DEFAULT_HERO_STATS_PATH
        payload = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_payload(payload, bracket=bracket)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any],
                     bracket: str = "ALL") -> "HeroStats":
        """从落盘的 JSON 结构构造（纯函数，测试用）。

        `bracket` 不存在时**退回 ALL**（再退到第一个分段）：换工具版本时
        不能让整条链路因为一个分段名对不上就静默失去英雄建议。
        """
        brackets = (payload or {}).get("brackets") or {}
        rows = brackets.get(bracket)
        used = bracket
        if rows is None:
            used = "ALL" if "ALL" in brackets else next(iter(brackets), bracket)
            rows = brackets.get(used, [])
        return cls.from_rows(rows, bracket=used,
                             fetched_at=str((payload or {}).get("fetched_at") or ""))

    @classmethod
    def from_rows(cls, rows: Sequence[Dict[str, Any]],
                  bracket: str = "ALL",
                  fetched_at: str = "") -> "HeroStats":
        out: Dict[str, HeroStat] = {}
        for r in rows or []:
            cid = str((r or {}).get("card_id") or "")
            if not cid:
                continue        # dbfId 没映射上 CardID 的行，跳过
            out[cid] = _row_to_stat(r)
        return cls(out, bracket=bracket, fetched_at=fetched_at)

    # -- 查询 / 打分 ------------------------------------------------------

    def get(self, card_id: str) -> Optional[HeroStat]:
        if not card_id:
            return None
        return self._rows.get(card_id)

    def score(self, card_id: str) -> Optional[HeroScore]:
        """百分位打分。查不到 `card_id` 时返回 None（调用方按"无数据"降级）。"""
        stat = self.get(card_id)
        if stat is None:
            return None
        pct: Optional[float] = None
        if stat.pick_rate is not None and len(self._rates) > 1:
            lower = sum(1 for v in self._rates if v < stat.pick_rate)
            pct = lower / (len(self._rates) - 1)
        # ⚠️ 不能写 `pct or 0.5` —— 0.0 是合法百分位（全表最低），会被当假值吃掉。
        score = round((0.5 if pct is None else pct) * SCORE_SCALE, 1)
        return HeroScore(score=score, pick_rate=stat.pick_rate,
                         armor=stat.armor, percentile=pct)

    def __len__(self) -> int:
        return len(self._rows)


def load_or_none(path: Optional[Path] = None,
                 bracket: str = "ALL") -> Optional[HeroStats]:
    """加载失败（文件缺失/损坏）返回 None，**不抛异常**。

    与本项目其它数据层的约定一致（缺 `cards_enUS.json` 时静默退回中文）：
    英雄建议是增量能力，绝不能因为它挂掉整条链路。
    """
    try:
        return HeroStats.load(path, bracket=bracket)
    except (OSError, ValueError, KeyError):
        return None
