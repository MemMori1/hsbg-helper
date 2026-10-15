#!/usr/bin/env python3
"""HSReplay 饰品实证数据层（`trinkets.py`）的单元测试（零依赖）。

⚠️ 与 `test_cards.py` 同一条约定：**不**依赖生成物 `data/trinkets_hsreplay.json`
（它由 `tools/fetch_hsreplay.py` 定期重拉，内容随补丁变）。这里用手搓样例行，
字段与真实文件完全一致。
"""

import json
import tempfile
from pathlib import Path

from hsbg.trinkets import (PLACEMENT_WEIGHT, REF_PLACEMENT, TIER_BASE,
                           TrinketStats, load_or_none)


def _row(card_id, tier, placement=None, pick=None, group="lesser"):
    row = {"card_id": card_id, "tier": tier, "group": group}
    if placement is not None:
        row["avg_final_placement"] = placement
    if pick is not None:
        row["pick_rate"] = pick
    return row


def test_score_is_tier_base_plus_placement_term():
    """`score = tier 基础分 + (4.2 − 平均名次) × 3` —— 公式变了要一起改测试。"""
    stats = TrinketStats.from_rows([_row("BG_X1", "s", 3.2, 46.0),
                                    _row("BG_X2", "f", 4.9, 10.0)])
    strong = stats.score("BG_X1")
    weak = stats.score("BG_X2")
    assert strong.score == round(TIER_BASE["s"] + (REF_PLACEMENT - 3.2)
                                 * PLACEMENT_WEIGHT, 2)
    assert weak.score == round(TIER_BASE["f"] + (REF_PLACEMENT - 4.9)
                               * PLACEMENT_WEIGHT, 2)
    assert strong.score > weak.score
    # 原始统计要原样带出来（展示层直接用）
    assert strong.tier == "s"
    assert strong.pick_rate == 46.0
    assert strong.avg_final_placement == 3.2


def test_notes_are_one_line_zh_and_en():
    sc = TrinketStats.from_rows([_row("BG_X1", "a", 3.9, 43.0)]).score("BG_X1")
    assert sc.note == "A级 · 选取43% · 平均名次3.90"
    # ADR-4：喂模型的一行必须全英文、无中文
    assert sc.note_en == "HSReplay tier A, picked 43%, avg place 3.90"
    assert all(ord(c) < 128 for c in sc.note_en)


def test_unknown_card_is_none_not_zero():
    """⚠️ 查不到必须是 None：当成 0 分会让新补丁的饰品看起来比 F 级还差。"""
    stats = TrinketStats.from_rows([_row("BG_X1", "s", 3.2, 46.0)])
    assert stats.score("BG_X2") is None
    assert stats.score("") is None
    assert stats.get("BG_X2") is None


def test_rows_without_card_id_are_skipped():
    """dbfId 没映射上 CardID 的行（拉取端的历史漏网）不能变成空键的脏数据。"""
    stats = TrinketStats.from_rows([{"tier": "s", "avg_final_placement": 3.0},
                                    _row("BG_X1", "a", 3.9, 43.0)])
    assert len(stats) == 1
    assert stats.score("") is None
    assert stats.get("BG_X1") is not None


def test_duplicate_rows_aggregate_to_the_median_placement_row():
    """⚠️ 多彩罗盘这类饰品是按**大厅种族组合**分行的（同一 card_id 10 行）。

    取中位数行作为代表值，并打上 `race_context` 标记 —— 大厅种族在日志里
    读不到，中位数是诚实的代表值（取最好 = 高估）。
    """
    rows = [_row("BG_X1", "s", 3.6, 40.0),
            _row("BG_X1", "b", 4.2, 29.0),
            _row("BG_X1", "f", 4.9, 12.0)]
    stat = TrinketStats.from_rows(rows).get("BG_X1")
    assert stat.race_context is True
    assert stat.avg_final_placement == 4.2      # 3 行取中间那行
    assert stat.tier == "b"
    sc = TrinketStats.from_rows(rows).score("BG_X1")
    assert "大厅种族相关" in sc.note
    assert "depends on lobby races" in sc.note_en


def test_single_row_has_no_race_context():
    stat = TrinketStats.from_rows([_row("BG_X1", "a", 3.9, 43.0)]).get("BG_X1")
    assert stat.race_context is False
    assert "大厅" not in TrinketStats.from_rows(
        [_row("BG_X1", "a", 3.9, 43.0)]).score("BG_X1").note


def test_rows_missing_placement_score_by_tier_only():
    """没有名次的行（HSReplay 极少数抽样不足）只给 tier 分，不能崩。"""
    sc = TrinketStats.from_rows([_row("BG_X1", "b")]).score("BG_X1")
    assert sc.score == TIER_BASE["b"]
    assert sc.avg_final_placement is None
    assert sc.note == "B级"


def test_from_payload_falls_back_to_the_all_bracket():
    """⚠️ 分段名对不上（工具换了分段口径）必须退回 ALL，而不是返回空表。"""
    payload = {"fetched_at": "2026-01-01T00:00:00Z",
               "brackets": {"ALL": [_row("BG_X1", "a", 3.9, 43.0)],
                            "TOP_1_PERCENT": [_row("BG_X1", "f", 4.6, 30.0)]}}
    fallback = TrinketStats.from_payload(payload, bracket="TOP_0_1_PERCENT")
    assert fallback.bracket == "ALL"
    assert fallback.get("BG_X1").tier == "a"
    top = TrinketStats.from_payload(payload, bracket="TOP_1_PERCENT")
    assert top.bracket == "TOP_1_PERCENT"
    assert top.get("BG_X1").tier == "f"


def test_load_reads_the_expected_shape():
    payload = {"fetched_at": "2026-01-01T00:00:00Z",
               "brackets": {"ALL": [_row("BG_X1", "a", 3.9, 43.0)]}}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "stats.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        stats = TrinketStats.load(p)
    assert stats.fetched_at == "2026-01-01T00:00:00Z"
    assert stats.bracket == "ALL"
    assert len(stats) == 1


def test_load_or_none_degrades_silently_on_bad_input():
    """缺文件 / 坏 JSON 都只返回 None —— 饰品建议是增量能力，不能拖垮主链路。"""
    with tempfile.TemporaryDirectory() as d:
        assert load_or_none(Path(d) / "nope.json") is None
        bad = Path(d) / "bad.json"
        bad.write_text("{ 这不是 JSON", encoding="utf-8")
        assert load_or_none(bad) is None
