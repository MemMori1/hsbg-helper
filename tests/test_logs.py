"""`hsbg.logs` 的单元测试：会话目录发现。

全部用临时目录造假会话，不依赖本机是否装了炉石。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg.logs import (  # noqa: E402
    SessionDir,
    latest_power_log,
    latest_session,
    list_sessions,
    newest_power_log_mtime,
    parse_session_name,
)

_GOOD = "Hearthstone_2026_10_10_21_11_09"


def _mk(root: Path, name: str, size: int = 0) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if size > 0:
        (d / "Power.log").write_bytes(b"x" * size)
    return d


def test_parse_session_name_ok():
    got = parse_session_name("Hearthstone_2026_10_10_21_11_09")
    assert got == datetime(2026, 10, 10, 21, 11, 9), got


def test_parse_session_name_rejects_other_shapes():
    for bad in (
        "Hearthstone_2026_10_10",           # 少段
        "Logs",                             # 完全无关
        "Hearthstone_2026_13_10_21_11_09",  # 月份非法 → strptime 抛错，必须返回 None
        "hearthstone_2026_10_10_21_11_09",  # 大小写敏感
        "Hearthstone_2026_10_10_21_11_09_x",  # 多尾巴
    ):
        assert parse_session_name(bad) is None, bad


def test_list_sessions_sorted_new_to_old_and_skips_noise():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root, "Hearthstone_2026_09_26_17_11_53", 10)
        _mk(root, "Hearthstone_2026_10_10_21_11_09", 20)
        _mk(root, "Hearthstone_2026_10_09_20_49_31", 30)
        _mk(root, "not_a_session")            # 无时间戳 → 跳过
        (root / "Power.log").write_text("文件不是目录 → 跳过")

        got = [s.name for s in list_sessions(root)]
        assert got == [
            "Hearthstone_2026_10_10_21_11_09",
            "Hearthstone_2026_10_09_20_49_31",
            "Hearthstone_2026_09_26_17_11_53",
        ], got


def test_list_sessions_missing_root_is_empty():
    assert list_sessions("/definitely/not/here/hsbg") == []


def test_session_dir_metadata():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = _mk(root, _GOOD, 1234)
        s = SessionDir(path=d, started_at=parse_session_name(_GOOD))
        assert s.has_power_log
        assert s.power_log == d / "Power.log"
        assert s.power_log_size() == 1234
        assert s.power_log_mtime() > 0
        assert "1.2 kB" not in str(s)  # 展示用格式，不做严格断言


def test_session_dir_without_power_log():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = _mk(root, _GOOD)  # 不建文件
        s = SessionDir(path=d, started_at=parse_session_name(_GOOD))
        assert not s.has_power_log
        assert s.power_log_size() == 0
        assert s.power_log_mtime() == 0.0


def test_latest_session_skips_sessions_without_power_log():
    """实测存在「刚创建、日志还没落盘」的会话目录，必须跳过。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root, "Hearthstone_2026_10_10_21_11_09", 20)   # 有日志
        _mk(root, "Hearthstone_2026_10_10_21_10_35")        # 更新但没日志
        got = latest_session(root)
        assert got is not None and got.name == "Hearthstone_2026_10_10_21_11_09", got


def test_latest_session_can_include_empty_sessions():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root, "Hearthstone_2026_10_10_21_10_35")           # 更旧、无日志
        _mk(root, "Hearthstone_2026_10_10_21_11_09", 20)       # 最新、有日志
        got = latest_session(root, require_power_log=False)
        assert got is not None and got.name == "Hearthstone_2026_10_10_21_11_09", got


def test_latest_power_log_and_mtime():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root, "Hearthstone_2026_10_09_20_49_31", 5)
        new = _mk(root, "Hearthstone_2026_10_10_21_11_09", 7)
        assert latest_power_log(root) == new / "Power.log"
        expected = os.stat(new / "Power.log").st_mtime
        assert newest_power_log_mtime(root) == expected


def test_latest_power_log_none_when_nothing_found():
    with tempfile.TemporaryDirectory() as td:
        assert latest_power_log(td) is None
