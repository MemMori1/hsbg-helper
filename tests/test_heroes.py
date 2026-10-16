#!/usr/bin/env python3
"""HSReplay 英雄实证数据层（`heroes.py`）的单元测试（零依赖）。

⚠️ 与 `test_trinkets.py` 同一条约定：**不**依赖生成物 `data/heroes_hsreplay.json`
（它由 `tools/fetch_hsreplay.py --heroes` 定期重拉，内容随补丁变）。
这里用手搓样例行，字段与真实文件完全一致。
"""

import json
import tempfile
from pathlib import Path

from hsbg.heroes import SCORE_SCALE, HeroStats, load_or_none


def _row(card_id, pick=None, armor=None, name="某英雄"):
    row = {"dbf_id": 12345, "card_id": card_id, "name": name}
    if pick is not None:
        row["pick_rate"] = pick
    if armor is not None:
        row["armor"] = armor
    return row


def test_score_is_the_pick_rate_percentile_on_a_0_to_10_scale():
    """`score = 10 × 选取率百分位` —— 公式变了要一起改测试。"""
    stats = HeroStats.from_rows([_row("H1", 80.0), _row("H2", 50.0),
                                 _row("H3", 20.0)])
    assert stats.score("H1").score == SCORE_SCALE * 1.0
    assert stats.score("H1").percentile == 1.0
    assert stats.score("H2").score == SCORE_SCALE * 0.5
    assert stats.score("H3").score == SCORE_SCALE * 0.0


def test_note_carries_pick_rate_and_armor():
    stats = HeroStats.from_rows([_row("H1", 77.6, 6, name="萨维斯")])
    sc = stats.score("H1")
    assert sc.note == "选取率78% · 护甲6"
    assert sc.note_en == "picked 78%, armor 6"


def test_missing_armor_is_omitted_from_the_note():
    """护甲缺失（老数据/新英雄）只显示选取率，不留 `护甲None` 这种垃圾文本。"""
    stats = HeroStats.from_rows([_row("H1", 33.0), _row("H2", 11.0)])
    assert stats.score("H1").note == "选取率33%"
    assert stats.score("H1").note_en == "picked 33%"


def test_unknown_card_returns_none_never_zero():
    """⚠️ 查不到必须返回 None：当作 0 分会让新英雄比最冷门的还差。"""
    stats = HeroStats.from_rows([_row("H1", 80.0), _row("H2", 20.0)])
    assert stats.score("BG36_HERO_999") is None
    assert stats.score("") is None


def test_rows_without_card_id_are_skipped():
    """dbfId 没映射上 CardID 的行（fetch 工具落盘为 null）直接跳过。"""
    stats = HeroStats.from_rows([dict(_row("H1", 80.0), card_id=None),
                                 _row("H2", 20.0)])
    assert len(stats) == 1
    assert stats.score("H2") is not None


def test_single_hero_table_scores_neutral():
    """表里只有一位英雄（理论情况）：百分位无意义，给中性 5.0，别假装最强。"""
    stats = HeroStats.from_rows([_row("H1", 80.0)])
    assert stats.score("H1").score == 5.0
    assert stats.score("H1").percentile is None


def test_hero_without_pick_rate_scores_neutral_but_keeps_armor():
    stats = HeroStats.from_rows([_row("H1", None, 12), _row("H2", 40.0)])
    sc = stats.score("H1")
    assert sc.score == 5.0
    assert sc.pick_rate is None and sc.armor == 12


def test_bracket_falls_back_to_all_then_to_the_first_one():
    payload = {
        "fetched_at": "2026-10-16T00:00:00Z",
        "brackets": {
            "ALL": [_row("H1", 80.0)],
            "TOP_1_PERCENT": [_row("H1", 60.0), _row("H2", 10.0)],
        },
    }
    got = HeroStats.from_payload(payload, bracket="TOP_1_PERCENT")
    assert got.bracket == "TOP_1_PERCENT" and got.score("H2") is not None
    # 分段名对不上 → 退回 ALL（最大样本）
    st = HeroStats.from_payload(payload, bracket="NOPE")
    assert st.bracket == "ALL" and st.score("H1") is not None
    # 连 ALL 都没有 → 退到第一个分段
    st2 = HeroStats.from_payload(
        {"brackets": {"TOP_1_PERCENT": [_row("H1", 60.0)]}}, bracket="ALL")
    assert st2.bracket == "TOP_1_PERCENT"


def test_load_reads_a_payload_from_disk():
    payload = {"fetched_at": "x",
               "brackets": {"ALL": [_row("H1", 80.0), _row("H2", 20.0)]}}
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "heroes.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        stats = HeroStats.load(p)
    assert stats.score("H1").score == 10.0
    assert stats.score("H2").score == 0.0
    assert stats.fetched_at == "x"


def test_load_or_none_degrades_on_missing_or_broken_file():
    """缺文件/坏 JSON 返回 None 不抛异常（英雄建议是增量能力，不许拖垮链路）。"""
    with tempfile.TemporaryDirectory() as td:
        assert load_or_none(Path(td) / "nope.json") is None
        broken = Path(td) / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        assert load_or_none(broken) is None
