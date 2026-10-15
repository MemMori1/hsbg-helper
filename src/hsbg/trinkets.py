#!/usr/bin/env python3
"""HSReplay 饰品实证数据：读取 + 打分。

数据由 `tools/fetch_hsreplay.py` 从 HSReplay 公开端点拉取，落盘
`data/trinkets_hsreplay.json`（255 行统计 = 237 件饰品的 tier / 选取率 / 平均名次；
另有 2 件按大厅种族分行，见下）。

**为什么要专门有这层**：规则层原来对「饰品四选一」刻意不打分 —— 420 张饰品
效果差异太大，靠卡文推断定不出权重（见 `rules.py` 的待选一节）。HSReplay
的这份统计是目前**唯一**可复现的实证来源：tier 是 HSReplay 按"选取率 + 平均
名次"综合出的档位，`avg_final_placement` 是真实对局打出来的名次。

打分公式（刻意简单、可解释）::

    score = tier 基础分 + (4.2 − 平均名次) × 3.0

- tier 给出跨档位的粗排序（S > A > B > C > D > F）。
- 平均名次在同一档位内做细分（同为 A 级，名次 3.7 的比 4.1 的高）。
- 参照点 4.2 是全体饰品的平均名次中位数（实测 ALL 分段），即"平均分"= 0 分。

⚠️ 数据来自 `brackets.ALL` 分段（默认）：它的样本最大，更贴近普通对局。
`TOP_1_PERCENT` 也能用（`load(bracket="TOP_1_PERCENT")`），但两套 tier 的
口径不同 —— 同一件饰品在 TOP_1% 里判 F 级的数量是 ALL 的 5 倍，混用会自相矛盾。

⚠️ 极少部分饰品（实测只有 `BG30_MagicItem_426` 多彩罗盘的小/大型两张）在
HSReplay 里是**按大厅种族组合**分行的（同一 `card_id` 10 行）。本模块按
**平均名次的中位数行**聚合，并标 `race_context=True`（展示时注明"大厅种族
相关"）—— 大厅的种族组合在日志里读不到，中位数是诚实的代表值，不是最优值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .cards import PACKAGE_DATA_DIR

__all__ = [
    "DEFAULT_TRINKET_STATS_PATH",
    "TIER_BASE",
    "REF_PLACEMENT",
    "PLACEMENT_WEIGHT",
    "TrinketStat",
    "TrinketScore",
    "TrinketStats",
    "load_or_none",
]

DEFAULT_TRINKET_STATS_PATH = PACKAGE_DATA_DIR / "trinkets_hsreplay.json"

#: tier → 基础分。每档差 1.5 分，只给粗排序，真正的细分交给平均名次。
TIER_BASE: Dict[str, float] = {
    "s": 3.0, "a": 1.5, "b": 0.5, "c": -0.5, "d": -1.5, "f": -3.0,
}

#: 平均名次的参照点：等于它得 0 分。实测 ALL 分段全体中位数 = 4.22。
REF_PLACEMENT = 4.2

#: 平均名次每领先 1 名值 3 分。实测聚合后全体名次区间 3.18–4.85（极差 1.67），
#: 因此名次项的实际贡献约 ±2.5 分 —— 与 tier 的 ±3 分同量级，不互相淹没。
PLACEMENT_WEIGHT = 3.0


@dataclass(frozen=True)
class TrinketStat:
    """一件饰品的实证统计（多行时是聚合后的代表行）。"""

    card_id: str
    name: str = ""
    group: str = ""                 # lesser / greater
    tier: str = ""                  # s/a/b/c/d/f
    #: 被 offer 时被选走的百分比（0–100）。
    pick_rate: Optional[float] = None
    #: 终局平均名次（越小越好，1 = 吃鸡）。
    avg_final_placement: Optional[float] = None
    #: True = 这件饰品在 HSReplay 里是**按大厅种族组合分行**的（多彩罗盘），
    #: 本条目是多行的中位数聚合，展示时要注明"大厅种族相关"。
    race_context: bool = False


@dataclass(frozen=True)
class TrinketScore:
    """打分结果：一个分数 + 组成它的原始统计（展示层按需取）。"""

    score: float
    tier: str = ""
    pick_rate: Optional[float] = None
    avg_final_placement: Optional[float] = None
    race_context: bool = False

    @property
    def note(self) -> str:
        """中文一行摘要（展示用）：`S级 · 选取46% · 平均名次3.55`。"""
        bits: List[str] = []
        if self.tier:
            bits.append("%s级" % self.tier.upper())
        if self.pick_rate is not None:
            bits.append("选取%.0f%%" % self.pick_rate)
        if self.avg_final_placement is not None:
            bits.append("平均名次%.2f" % self.avg_final_placement)
        if self.race_context:
            bits.append("大厅种族相关")
        return " · ".join(bits)

    @property
    def note_en(self) -> str:
        """英文一行摘要（喂模型用，ADR-4：题面必须全英文）。"""
        bits: List[str] = []
        if self.tier:
            bits.append("HSReplay tier %s" % self.tier.upper())
        if self.pick_rate is not None:
            bits.append("picked %.0f%%" % self.pick_rate)
        if self.avg_final_placement is not None:
            bits.append("avg place %.2f" % self.avg_final_placement)
        text = ", ".join(bits)
        if self.race_context:
            text += ", depends on lobby races" if text else "depends on lobby races"
        return text


def _row_to_stat(row: Dict[str, Any], race_context: bool = False) -> TrinketStat:
    def num(key: str) -> Optional[float]:
        v = row.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return TrinketStat(
        card_id=str(row.get("card_id") or ""),
        name=str(row.get("name") or ""),
        group=str(row.get("group") or ""),
        tier=str(row.get("tier") or "").lower(),
        pick_rate=num("pick_rate"),
        avg_final_placement=num("avg_final_placement"),
        race_context=race_context,
    )


def _merge_rows(rows: Sequence[Dict[str, Any]]) -> TrinketStat:
    """同一 `card_id` 的多行聚合成一行（大厅种族相关的那两张）。

    ⚠️ 取**平均名次的中位数行**（而不是最好/最差/逐项平均）：
    - 取最好 = 高估（假设大厅种族正好是它的最佳组合）；
    - 逐项取平均 = tier 没有"平均档"可对应，展示层会说谎；
    - 中位数 = 不管大厅是什么，它是"典型大厅"下的表现，标注后可解释。
    """
    if len(rows) == 1:
        return _row_to_stat(rows[0])
    ordered = sorted(
        rows,
        key=lambda r: (r.get("avg_final_placement") is None,
                       r.get("avg_final_placement") or 0.0),
    )
    return _row_to_stat(ordered[len(ordered) // 2], race_context=True)


class TrinketStats:
    """`card_id → TrinketStat` 查询表 + 打分。

    用法::

        stats = TrinketStats.load()           # data/trinkets_hsreplay.json
        sc = stats.score("BG36_MagicItem_307")
        sc.score, sc.note                     # 3.9, "A级 · 选取43% · 平均名次3.90"

    ⚠️ 查不到返回 `None`（新补丁刚出的饰品 HSReplay 还没统计到），调用方必须按
    "没有数据"降级 —— 绝不能把它当 0 分（那会让新饰品看起来比 D 级还差）。
    """

    def __init__(self, rows: Dict[str, TrinketStat],
                 bracket: str = "ALL", fetched_at: str = "") -> None:
        self._rows = rows
        self.bracket = bracket
        self.fetched_at = fetched_at

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None,
             bracket: str = "ALL") -> "TrinketStats":
        p = Path(path) if path is not None else DEFAULT_TRINKET_STATS_PATH
        payload = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_payload(payload, bracket=bracket)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any],
                     bracket: str = "ALL") -> "TrinketStats":
        """从落盘的 JSON 结构构造（纯函数，测试用）。

        `bracket` 不存在时**退回 ALL**（再退到第一个分段）：换工具版本时
        不能让整条链路因为一个分段名对不上就静默失去饰品建议。
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
                  fetched_at: str = "") -> "TrinketStats":
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows or []:
            cid = str((r or {}).get("card_id") or "")
            if not cid:
                continue        # dbfId 没映射上 CardID 的行，跳过
            grouped.setdefault(cid, []).append(r)
        return cls({cid: _merge_rows(g) for cid, g in grouped.items()},
                   bracket=bracket, fetched_at=fetched_at)

    # -- 查询 / 打分 ------------------------------------------------------

    def get(self, card_id: str) -> Optional[TrinketStat]:
        if not card_id:
            return None
        return self._rows.get(card_id)

    def score(self, card_id: str) -> Optional[TrinketScore]:
        """打分。查不到 `card_id` 时返回 None（调用方按"无数据"降级）。"""
        stat = self.get(card_id)
        if stat is None:
            return None
        s = TIER_BASE.get(stat.tier, 0.0)
        if stat.avg_final_placement is not None:
            s += (REF_PLACEMENT - stat.avg_final_placement) * PLACEMENT_WEIGHT
        return TrinketScore(
            score=round(s, 2),
            tier=stat.tier,
            pick_rate=stat.pick_rate,
            avg_final_placement=stat.avg_final_placement,
            race_context=stat.race_context,
        )

    def __len__(self) -> int:
        return len(self._rows)


def load_or_none(path: Optional[Path] = None,
                 bracket: str = "ALL") -> Optional[TrinketStats]:
    """加载失败（文件缺失/损坏）返回 None，**不抛异常**。

    与本项目其它数据层的约定一致（缺 `cards_enUS.json` 时静默退回中文）：
    饰品建议是增量能力，绝不能因为它挂掉整条链路。
    """
    try:
        return TrinketStats.load(path, bracket=bracket)
    except (OSError, ValueError, KeyError):
        return None
