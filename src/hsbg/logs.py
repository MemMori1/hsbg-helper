"""日志目录发现与会话扫描（P0）。

炉石每次启动会新建一个会话目录（实测 6 个）::

    /Applications/Hearthstone/Logs/
      ├── Hearthstone_2026_10_10_21_11_09/   <- 最新，Power.log 92 MB
      ├── Hearthstone_2026_10_09_20_49_31/   <- 次新，Power.log 100 MB
      └── ...

⚠️ `log.config` 在本机**不存在**，`client.config` 里 `FileSizeLimit.Int=-1`
意味着**日志不轮转、无限增长**。所以这里只做"发现 + 元信息"，不假设大小上限。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import POWER_LOG_NAME, SESSION_DIR_PREFIX

#: `Hearthstone_2026_10_10_21_11_09`
_SESSION_RE = re.compile(
    r"^" + re.escape(SESSION_DIR_PREFIX) + r"(?P<stamp>\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})$"
)

#: 会话目录名里的时间戳格式
_STAMP_FMT = "%Y_%m_%d_%H_%M_%S"


@dataclass(frozen=True)
class SessionDir:
    """一个日志会话目录。"""

    path: Path
    started_at: Optional[datetime]

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def power_log(self) -> Path:
        return self.path / POWER_LOG_NAME

    @property
    def has_power_log(self) -> bool:
        return self.power_log.is_file()

    def power_log_size(self) -> int:
        try:
            return self.power_log.stat().st_size
        except OSError:
            return 0

    def power_log_mtime(self) -> float:
        try:
            return self.power_log.stat().st_mtime
        except OSError:
            return 0.0

    def __str__(self) -> str:  # pragma: no cover - 展示用
        size = self.power_log_size()
        mb = f"{size / 1e6:.1f} MB" if size else "无"
        when = self.started_at.strftime("%Y-%m-%d %H:%M:%S") if self.started_at else "?"
        return f"{self.name}  {when}  Power.log={mb}"


def parse_session_name(name: str) -> Optional[datetime]:
    """把会话目录名解析成启动时间。解析不出来返回 None。"""
    m = _SESSION_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group("stamp"), _STAMP_FMT)
    except ValueError:
        return None


def list_sessions(log_root: str | Path) -> List[SessionDir]:
    """列出所有会话目录，**按启动时间新→旧排序**。

    排序依据：优先用目录名里的时间戳；解析不出来时退化为 mtime。
    只返回目录（跳过文件与无关目录）。
    """
    root = Path(log_root)
    if not root.is_dir():
        return []

    sessions: List[SessionDir] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        started = parse_session_name(entry.name)
        if started is None:
            continue
        sessions.append(SessionDir(path=entry, started_at=started))

    sessions.sort(key=lambda s: (s.started_at or datetime.min), reverse=True)
    return sessions


def latest_session(log_root: str | Path, require_power_log: bool = True) -> Optional[SessionDir]:
    """最新的会话目录。

    `require_power_log=True` 时跳过没有 `Power.log` 的目录——实测存在刚创建、
    日志还没落盘的会话目录，直接返回它会让 tailer 空转。
    """
    for s in list_sessions(log_root):
        if not require_power_log or s.has_power_log:
            return s
    return None


def latest_power_log(log_root: str | Path) -> Optional[Path]:
    """最新会话的 `Power.log` 路径。"""
    s = latest_session(log_root)
    return s.power_log if s else None


def newest_power_log_mtime(log_root: str | Path) -> float:
    """所有会话里最新的 `Power.log` 修改时间（用来判断游戏是否还在写）。"""
    best = 0.0
    for s in list_sessions(log_root):
        best = max(best, s.power_log_mtime())
    return best


__all__ = [
    "SessionDir",
    "parse_session_name",
    "list_sessions",
    "latest_session",
    "latest_power_log",
    "newest_power_log_mtime",
]
