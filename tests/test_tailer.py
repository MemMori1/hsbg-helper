"""`hsbg.tailer` 的单元测试：半行缓冲、截断、会话轮换。

关键点：tailer 必须只把**完整行**交给解析器。日志是游戏进程边写边被读的，
最后一截 fragment 若是直接喂进去，会产生半个事件（实测真实日志每行 100+ 字节，
被截断的概率不低）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg.config import TailConfig  # noqa: E402
from hsbg.events import EventType  # noqa: E402
from hsbg.tailer import LogTailer, TailerStatus  # noqa: E402

_HEAD = "D 21:12:25.2665920 GameState.DebugPrintPower() - "


def _line(payload: str, indent: int = 0) -> str:
    return _HEAD + " " * indent + payload + "\n"


def _cfg() -> TailConfig:
    # 轮询间隔压到极小，测试不想真等 0.1 s
    return TailConfig(poll_interval=0.001)


def test_open_missing_root_reports_no_log_root():
    t = LogTailer(log_root="/definitely/not/here/hsbg", config=_cfg())
    assert t.open() is False
    assert t.status == TailerStatus.NO_LOG_ROOT
    t.close()


def test_open_root_without_sessions_reports_waiting():
    with tempfile.TemporaryDirectory() as td:
        t = LogTailer(log_root=td, config=_cfg())
        assert t.open() is False
        assert t.status == TailerStatus.WAITING_SESSION
        t.close()


def test_open_path_reads_from_start_when_at_end_false():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Power.log"
        p.write_text(_line("CREATE_GAME"))
        t = LogTailer(log_root=td, config=_cfg())
        t.open_path(p, at_end=False)
        evs = t.poll()
        assert [e.kind for e in evs] == [EventType.CREATE_GAME], evs
        t.close()


def test_at_end_skips_existing_content_then_reads_appends():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Power.log"
        p.write_text(_line("CREATE_GAME"))
        t = LogTailer(log_root=td, config=_cfg())
        t.open_path(p, at_end=True)
        assert t.poll() == []           # 已有内容被跳过
        with open(p, "a") as fh:
            fh.write(_line("CREATE_GAME"))
        evs = t.poll()
        assert [e.kind for e in evs] == [EventType.CREATE_GAME], evs
        t.close()


def test_half_line_is_buffered_until_newline_arrives():
    """核心用例：写到一半的行不能被解析。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Power.log"
        p.write_text("")
        t = LogTailer(log_root=td, config=_cfg())
        t.open_path(p, at_end=False)

        full = _line("CREATE_GAME")
        with open(p, "w") as fh:
            fh.write(full[: len(full) // 2])     # 半行
        assert t.poll() == [], "半行不能产出事件"
        assert t._buf, "半行必须留在缓冲里"

        with open(p, "a") as fh:
            fh.write(full[len(full) // 2 :])     # 补上后半
        evs = t.poll()
        assert [e.kind for e in evs] == [EventType.CREATE_GAME], evs
        assert t._buf == b""
        t.close()


def test_multiple_lines_in_one_poll():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Power.log"
        p.write_text("")
        t = LogTailer(log_root=td, config=_cfg())
        t.open_path(p, at_end=False)
        with open(p, "a") as fh:
            fh.write(_line("CREATE_GAME"))
            fh.write(_line("FULL_ENTITY - Creating ID=73 CardID=TB_BaconShop_HERO_KelThuzad"))
            fh.write(_line("TAG_CHANGE Entity=GameEntity tag=TURN value=1"))
        kinds = [e.kind for e in t.poll()]
        assert kinds == [EventType.CREATE_GAME, EventType.FULL_ENTITY, EventType.TAG_CHANGE], kinds
        t.close()


def test_truncation_resets_offset():
    """文件变小 = 游戏重启重写了 Power.log，必须把 offset 归零重读。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Power.log"
        p.write_text(_line("CREATE_GAME") * 4)
        t = LogTailer(log_root=td, config=_cfg())
        t.open_path(p, at_end=False)
        assert len(t.poll()) == 4

        p.write_text(_line("CREATE_GAME"))       # 变小 → 截断
        evs = t.poll()
        assert t.stats.truncations == 1
        assert [e.kind for e in evs] == [EventType.CREATE_GAME], evs
        assert t.offset == len(_line("CREATE_GAME").encode())
        t.close()


def test_idle_when_no_new_bytes():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Power.log"
        p.write_text(_line("CREATE_GAME"))
        t = LogTailer(log_root=td, config=_cfg())
        t.open_path(p, at_end=False)
        t.poll()
        assert t.poll() == []
        assert t.status == TailerStatus.IDLE
        t.close()


def test_switches_to_newer_session_directory():
    """新会话目录出现时必须切过去，否则实时建议会停在旧文件上。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old = root / "Hearthstone_2026_10_10_21_11_09"
        old.mkdir()
        (old / "Power.log").write_text(_line("CREATE_GAME"))
        t = LogTailer(log_root=root, config=_cfg())
        assert t.open()
        assert t.current_path == old / "Power.log"
        t.poll()

        new = root / "Hearthstone_2026_10_11_09_00_00"
        new.mkdir()
        (new / "Power.log").write_text(_line("CREATE_GAME") * 2)

        # 切换动作本身不产事件（新文件按 at_end=True 打开，忽略已有内容）
        assert t.poll() == []
        assert t.current_path == new / "Power.log"
        assert t.status == TailerStatus.SESSION_SWITCH
        assert t.stats.sessions_followed == 1
        # 切换后追加的**新**内容才产出
        with open(new / "Power.log", "a") as fh:
            fh.write(_line("CREATE_GAME"))
        evs = t.poll()
        assert [e.kind for e in evs] == [EventType.CREATE_GAME], evs
        t.close()


def test_does_not_switch_back_to_older_session():
    """已经在最新会话上时不能来回跳。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        older = root / "Hearthstone_2026_10_09_20_49_31"
        older.mkdir()
        (older / "Power.log").write_text(_line("CREATE_GAME"))
        newer = root / "Hearthstone_2026_10_10_21_11_09"
        newer.mkdir()
        (newer / "Power.log").write_text(_line("CREATE_GAME"))
        t = LogTailer(log_root=root, config=_cfg())
        assert t.open()
        assert t.current_path == newer / "Power.log"
        assert t.poll() == []
        assert t.current_path == newer / "Power.log", "不能在会话间来回跳"
        assert t.stats.sessions_followed == 0
        t.close()


def test_follow_yields_then_stops_after_idle_polls():
    """`follow()` 从已有内容开始读，空闲若干轮后退出。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sess = root / "Hearthstone_2026_10_10_21_11_09"
        sess.mkdir()
        (sess / "Power.log").write_text(_line("CREATE_GAME"))
        # ⚠️ follow() 内部会调 open() 重新发现最新会话，所以必须给出真实的会话目录，
        # 不能用 open_path —— 那会被 open() 覆盖掉，变成空转。
        t = LogTailer(log_root=root, config=_cfg(), start_at_end=False)
        got = list(t.follow(max_idle_polls=3))
        assert [e.kind for e in got] == [EventType.CREATE_GAME], got
        t.close()


def test_stats_accumulate():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Power.log"
        p.write_text("")
        t = LogTailer(log_root=td, config=_cfg())
        t.open_path(p, at_end=False)
        with open(p, "a") as fh:
            fh.write(_line("CREATE_GAME"))
        t.poll()
        assert t.stats.lines_read == 1
        assert t.stats.events == 1
        assert t.stats.bytes_read > 0
        d = t.stats.as_dict()
        assert d["lines_read"] == 1 and d["events"] == 1
        t.close()
