#!/usr/bin/env python3
"""复盘回放（`replay.py`）的单元测试（零依赖）。

约定与其它测试一致：**不**依赖真实日志与 `replays/` 目录 —— 全部用
手搓帧 + 临时目录，任何机器上都能离线跑。
"""

import io
import json
import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg.cli import main                                    # noqa: E402
from hsbg.replay import (ReplayRecorder, list_replays,        # noqa: E402
                         load_replay, render_replay, resolve_target)
from hsbg.state import ChoiceRecord                          # noqa: E402


def _frame(turn, gold=3, health=40, tier=1, shop=None, decision=None,
           pending=None):
    """最小可用的面板帧（字段名与 `PanelFrame.to_dict()` 对齐）。"""
    f = {
        "turn": turn, "phase": "recruit", "gold": gold, "health": health,
        "tavern_tier": tier, "headline": "", "shop": shop or [], "spells": [],
        "board": [], "hand": [], "opponents": [], "warnings": [],
        "model_answers": [], "model_note": "", "stale": False,
    }
    if decision is not None:
        f["decision"] = decision
    if pending is not None:
        f["pending_choice"] = pending
    return f


def _choice(kind="small_trinket", name="朝圣小鬼标签"):
    return ChoiceRecord(choice_id=1, kind=kind, source_name="小饰品",
                        selected_name=name, selected_card_id="BG_X1")


def test_recorder_rotates_on_turn_reset_and_keeps_meta():
    """回合回卷 = 新局：旧局先落盘（`partial=False`），新局另开一个文件。"""
    with tempfile.TemporaryDirectory() as d:
        rec = ReplayRecorder(out_dir=Path(d))
        rec.note(_frame(1), choices=[_choice()],
                 hero="钩牙船长", hero_card_id="TB_BaconShop_HERO_91")
        rec.note(_frame(2, gold=7, health=38))
        rec.note(_frame(3, gold=9, health=35))
        first = rec.note(_frame(1, gold=3, health=35))       # ← 新局第一帧
        assert first is not None and first.exists()
        doc = json.loads(first.read_text(encoding="utf-8"))
        assert doc["hero"] == "钩牙船长"
        assert doc["hero_card_id"] == "TB_BaconShop_HERO_91"
        assert doc["partial"] is False                       # 看到新局 = 已完结
        assert doc["turns"] == 3
        assert doc["final"] == {"turn": 3, "health": 35}
        assert [f["turn"] for f in doc["frames"]] == [1, 2, 3]
        assert doc["choices"][0]["selected_name"] == "朝圣小鬼标签"
        # 新局还在进行中：flush 落第二个文件，partial=True
        second = rec.flush()
        assert second is not None and second != first
        doc2 = json.loads(second.read_text(encoding="utf-8"))
        assert doc2["partial"] is True
        assert [f["turn"] for f in doc2["frames"]] == [1]


def test_recorder_dedups_identical_frames():
    """面板每 0.4s 无条件出帧：全同的帧只留一份，否则一局十几 MB。"""
    with tempfile.TemporaryDirectory() as d:
        rec = ReplayRecorder(out_dir=Path(d))
        same = _frame(2, gold=5, health=39)
        rec.note(same)
        rec.note(dict(same))
        rec.note(dict(same))
        rec.note(_frame(2, gold=4, health=39))               # 花了 1 费 → 变化
        path = rec.flush()
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert len(doc["frames"]) == 2
        assert doc["frames"][0]["gold"] == 5
        assert doc["frames"][1]["gold"] == 4


def test_recorder_ignores_pregame_frames():
    """选人/载入帧（turn=0）没有盘面，不入档；空录制 flush 不产文件。"""
    with tempfile.TemporaryDirectory() as d:
        rec = ReplayRecorder(out_dir=Path(d))
        assert rec.note(_frame(0), hero="钩牙船长") is None
        assert rec.note({"turn": None, "gold": 0}) is None
        assert rec.flush() is None
        assert list(Path(d).glob("*.json")) == []


def test_recorder_disabled_writes_nothing():
    with tempfile.TemporaryDirectory() as d:
        rec = ReplayRecorder(out_dir=Path(d), enabled=False)
        rec.note(_frame(3))
        assert rec.flush() is None
        assert list(Path(d).glob("*.json")) == []


def test_recorder_checkpoints_midgame():
    """进行中也周期性落盘：悬浮框进程被 SIGTERM 杀掉时不能整局白录。"""
    import hsbg.replay as R
    old = R.CHECKPOINT_SECONDS
    try:
        R.CHECKPOINT_SECONDS = 0.0            # 每帧都 checkpoint，好断言
        with tempfile.TemporaryDirectory() as d:
            rec = ReplayRecorder(out_dir=Path(d))
            rec.note(_frame(1, gold=3), hero="钩牙船长")
            files = list(Path(d).glob("*.json"))
            assert len(files) == 1            # 还没 flush，文件已在
            doc = json.loads(files[0].read_text(encoding="utf-8"))
            assert doc["partial"] is True and doc["turns"] == 1
            rec.note(_frame(2, gold=6))
            doc = json.loads(files[0].read_text(encoding="utf-8"))
            assert doc["turns"] == 2          # 同文件被原子重写
    finally:
        R.CHECKPOINT_SECONDS = old


def test_recorder_never_raises_on_bad_io():
    """写不进目录（路径是个文件）→ 静默放弃，不能让面板崩。"""
    with tempfile.TemporaryDirectory() as d:
        blocked = Path(d) / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        rec = ReplayRecorder(out_dir=blocked)
        rec.note(_frame(1))                  # 不能抛
        assert rec.flush() is None


def test_list_replays_newest_first_and_skips_corrupt():
    with tempfile.TemporaryDirectory() as d:
        rec = ReplayRecorder(out_dir=Path(d))
        rec.note(_frame(1), hero="甲")
        rec.flush()                          # 先落盘的
        rec.note(_frame(1), hero="乙")
        rec.flush()                          # 后落盘的
        (Path(d) / "broken.json").write_text("{ not json", encoding="utf-8")
        (Path(d) / "other.json").write_text('{"hello": 1}', encoding="utf-8")
        items = list_replays(Path(d))
        assert len(items) == 2               # 两个坏文件被跳过
        assert {it["hero"] for it in items} == {"甲", "乙"}
        assert all(it["frames"] == 1 for it in items)


def test_resolve_target_by_index_last_and_path():
    with tempfile.TemporaryDirectory() as d:
        rec = ReplayRecorder(out_dir=Path(d))
        rec.note(_frame(1), hero="甲")
        rec.flush()
        rec.note(_frame(1), hero="乙")
        rec.flush()
        items = list_replays(Path(d))
        assert len(items) == 2
        # 同秒落盘时"谁最新"按文件名兜底，自洽即可：0 == last == items[0]
        assert resolve_target("last", Path(d)) == items[0]["path"]
        assert resolve_target("0", Path(d)) == items[0]["path"]
        assert resolve_target("-1", Path(d)) == items[1]["path"]
        assert resolve_target(str(items[0]["path"]), Path(d)) == items[0]["path"]
        try:
            resolve_target("9", Path(d))
            raise AssertionError("超范围序号应当报错")
        except FileNotFoundError:
            pass
        try:
            resolve_target(str(Path(d) / "nope.json"), Path(d))
            raise AssertionError("不存在的文件应当报错")
        except FileNotFoundError:
            pass


def test_render_replay_shows_turns_decision_and_choices():
    doc = {
        "version": 1, "hero": "钩牙船长", "started_at": "2026-10-16 19:40:12",
        "ended_at": "2026-10-16 20:05:33", "partial": False, "turns": 2,
        "final": {"turn": 2, "health": -3},
        "choices": [{"kind": "small_trinket", "source_name": "小饰品",
                     "selected_name": "朝圣小鬼标签", "selected_card_id": "BG_X1"}],
        "frames": [
            _frame(1, gold=3, shop=[{"name": "鱼人招潮者"}, {"name": "石塘猎人"}]),
            _frame(2, gold=5, health=36, decision={"action": "买", "target": "铜须"}),
            _frame(2, gold=2, health=36, pending={
                "kind": "trinket", "title": "小饰品 4 选 1",
                "options": [{"name": "朝圣小鬼标签", "recommended": True},
                            {"name": "便携酒柜", "recommended": False}]}),
        ],
    }
    text = render_replay(doc, color=False)
    assert "钩牙船长" in text and "2 回合" in text
    assert "小饰品 → 「朝圣小鬼标签」" in text
    assert "── 回合 1 ──" in text and "── 回合 2 ──" in text
    assert "决策：买「铜须」" in text
    assert "待选：小饰品 4 选 1（★ 朝圣小鬼标签）" in text
    assert "[0m" not in text                 # 无色模式不该混进 ANSI


def test_load_replay_rejects_foreign_json():
    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "bad.json"
        bad.write_text('{"frames": "not-a-list"}', encoding="utf-8")
        try:
            load_replay(bad)
            raise AssertionError("坏录像应当抛 ValueError")
        except ValueError:
            pass


def test_cli_replay_lists_empty_dir_and_rejects_bad_target():
    """CLI 冒烟：空目录列不出来但不报错；坏序号返回 1。"""
    def quiet(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return main(argv), buf.getvalue()

    with tempfile.TemporaryDirectory() as d:
        rc, out = quiet(["replay", "--replay-dir", d])
        assert rc == 0
        assert "还没录到对局" in out
        rc, _ = quiet(["replay", "0", "--replay-dir", d])
        assert rc == 1


def test_cli_replay_renders_a_recorded_game():
    with tempfile.TemporaryDirectory() as d:
        rec = ReplayRecorder(out_dir=Path(d))
        rec.note(_frame(1, shop=[{"name": "石塘猎人"}]), hero="钩牙船长")
        rec.note(_frame(2, decision={"action": "升本"}))
        rec.flush()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main(["replay", "0", "--replay-dir", d, "--no-color"])
        assert rc == 0
        out = buf.getvalue()
        assert "钩牙船长" in out and "决策：升本" in out
