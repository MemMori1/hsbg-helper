"""增量 tail：把 `Power.log` 的**新增**内容变成事件流（P0）。

为什么不用 `watchdog`：本机没装，而项目原则是**零第三方依赖**
（对齐 `auxac-mac` 的做法）。日志文件每局近 100 MB，0.1 s 的轮询
只看 `stat()` 大小，代价可以忽略。

三个必须处理的真实情况：

1. **半行**：进程随时可能在写到一半时被我们读到，`read` 拿到的最后一个
   fragment 必须留到下一轮再拼。绝不能直接把 fragment 喂给解析器。
2. **轮换/截断**：文件大小突然**变小**说明游戏重启并重写了 `Power.log`，
   此时必须把 offset 归零。
3. **新会话目录**：游戏重启会新建 `Hearthstone_<时间戳>/`。
   `follow_new_session=True` 时自动切过去（否则一直读旧文件，实时建议会静止）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, List, Optional

from .config import TailConfig
from .events import PowerEvent
from .logs import latest_session, parse_session_name
from .parser import PowerLogParser


class TailerStatus(str, Enum):
    """tailer 的状态，供 CLI/UI 展示与排障。"""

    OK = "ok"                        # 正常跟随
    NO_LOG_ROOT = "no_log_root"      # 日志根目录不存在（炉石没装/路径不对）
    WAITING_SESSION = "waiting_session"  # 目录在，但没有可用的会话
    SESSION_SWITCH = "session_switch"    # 刚刚切换到新会话
    TRUNCATED = "truncated"              # 检测到文件被截断/重写
    IDLE = "idle"                        # 跟上了但暂时没有新数据


@dataclass
class TailerStats:
    """运行统计。"""

    bytes_read: int = 0
    lines_read: int = 0
    events: int = 0
    sessions_followed: int = 0
    truncations: int = 0
    started_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "bytes_read": self.bytes_read,
            "lines_read": self.lines_read,
            "events": self.events,
            "sessions_followed": self.sessions_followed,
            "truncations": self.truncations,
        }


class LogTailer:
    """跟随最新会话的 `Power.log`，增量产出 `PowerEvent`。

    用法::

        tailer = LogTailer()
        for ev in tailer.follow():      # 阻塞式生成器，可 Ctrl-C 中断
            handle(ev)

    也可以手动泵::

        tailer.open()
        evs = tailer.poll()             # 非阻塞，返回这一轮读到的事件
    """

    def __init__(
        self,
        log_root: str | Path | None = None,
        config: Optional[TailConfig] = None,
        start_at_end: bool = True,
    ) -> None:
        from .config import DEFAULT_LOG_ROOT

        self.log_root = Path(log_root or DEFAULT_LOG_ROOT)
        self.cfg = config or TailConfig()
        #: True 时忽略文件已有内容（只看"从现在开始"的新事件）。
        #: 历史回归要用 start_at_end=False 从头读。
        self.start_at_end = start_at_end

        self.parser = PowerLogParser()
        self.stats = TailerStats()
        self.status = TailerStatus.IDLE

        self._path: Optional[Path] = None
        self._fh = None
        self._offset = 0          # 字节偏移
        self._size = 0            # 上次见到的文件大小
        self._buf = b""           # 未成行的尾巴

    # -- 基本属性 -----------------------------------------------------------

    @property
    def current_path(self) -> Optional[Path]:
        return self._path

    @property
    def offset(self) -> int:
        return self._offset

    # -- 生命周期 -----------------------------------------------------------

    def open(self) -> bool:
        """打开最新会话的 `Power.log`。返回是否成功。"""
        self._close_file()
        session = latest_session(self.log_root)
        if session is None:
            self.status = (
                TailerStatus.NO_LOG_ROOT
                if not self.log_root.is_dir()
                else TailerStatus.WAITING_SESSION
            )
            return False
        self._switch_session(session.power_log, initial=True)
        return True

    def open_path(self, path: str | Path, at_end: bool = False) -> None:
        """直接指定一个文件（历史日志回归用）。"""
        self._close_file()
        p = Path(path)
        self._switch_session(p, initial=True, at_end=at_end)

    def close(self) -> None:
        self._close_file()

    def _close_file(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def _switch_session(self, path: Path, initial: bool = False, at_end: bool | None = None) -> None:
        """切换到新的日志文件，重置偏移与半行缓冲。"""
        self._close_file()
        self._path = path
        self._buf = b""
        try:
            self._fh = open(path, "rb")
        except OSError:
            self._fh = None
            self.status = TailerStatus.WAITING_SESSION
            return

        use_end = self.start_at_end if at_end is None else at_end
        if use_end:
            try:
                self._size = path.stat().st_size
            except OSError:
                self._size = 0
            self._offset = self._size
            self._fh.seek(self._offset)
        else:
            self._size = 0
            self._offset = 0

        if initial:
            self.stats.started_at = time.time()
        else:
            self.stats.sessions_followed += 1
            self.status = TailerStatus.SESSION_SWITCH

    # -- 轮询 ---------------------------------------------------------------

    def poll(self) -> List[PowerEvent]:
        """读一轮新增内容，返回事件。非阻塞。

        调用方负责节奏控制（`follow()` 会自动 sleep）。
        """
        # 1. 检查是否需要切换到更新的会话目录。
        if self.cfg.follow_new_session and self._maybe_switch_session():
            return []  # 切换动作本身不产事件，下一轮再读

        if self._fh is None:
            if not self.open():
                return []

        assert self._path is not None and self._fh is not None

        # 2. 文件变小 => 被截断/重写。
        try:
            size = self._path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            self.stats.truncations += 1
            self.status = TailerStatus.TRUNCATED
            self._offset = 0
            self._buf = b""
            self._fh.seek(0)
            size = self._path.stat().st_size if self._path.exists() else 0

        if size == self._offset:
            if self.status not in (TailerStatus.SESSION_SWITCH, TailerStatus.TRUNCATED):
                self.status = TailerStatus.IDLE
            return []

        # 3. 读新增字节。
        self._fh.seek(self._offset)
        raw = self._fh.read(min(self.cfg.read_chunk, size - self._offset))
        if not raw:
            return []
        self._offset += len(raw)
        self.stats.bytes_read += len(raw)

        # 4. 拼半行：只把**完整行**交给解析器。
        data = self._buf + raw
        if data.endswith(b"\n"):
            complete, self._buf = data, b""
        else:
            idx = data.rfind(b"\n")
            if idx < 0:
                self._buf = data
                return []
            complete, self._buf = data[: idx + 1], data[idx + 1 :]

        text = complete.decode("utf-8", errors="replace")
        events: List[PowerEvent] = []
        for line in text.splitlines():
            if not line:
                continue
            self.stats.lines_read += 1
            events.extend(self.parser.feed_line(line))

        self.stats.events += len(events)
        if events:
            self.status = TailerStatus.OK
        return events

    def _maybe_switch_session(self) -> bool:
        """若出现了比当前更新的会话，切过去。返回是否发生了切换。"""
        session = latest_session(self.log_root)
        if session is None:
            return False
        target = session.power_log
        if self._path is not None and target == self._path:
            return False
        # 只有当新会话的启动时间晚于当前会话时才切换，
        # 避免在多个目录之间来回跳。
        cur_stamp = parse_session_name(self._path.parent.name) if self._path else None
        new_stamp = session.started_at
        if cur_stamp is not None and new_stamp is not None and new_stamp <= cur_stamp:
            return False
        self._switch_session(target, initial=False, at_end=True)
        return True

    # -- 阻塞式跟随 ---------------------------------------------------------

    def follow(self, max_idle_polls: Optional[int] = None) -> Iterator[PowerEvent]:
        """阻塞式跟随，持续产出事件。

        `max_idle_polls` 用于测试/脚本：连续这么多轮没数据就退出。
        CLI 实时模式不传（永远跟下去，直到 Ctrl-C）。
        """
        if not self.open():
            # 炉石还没开：等目录出现。
            if self.cfg.follow_new_session:
                idle = 0
                while True:
                    time.sleep(self.cfg.poll_interval)
                    if self.open():
                        break
                    idle += 1
                    if max_idle_polls is not None and idle >= max_idle_polls:
                        return
            else:
                return

        idle = 0
        while True:
            events = self.poll()
            if events:
                idle = 0
                for ev in events:
                    yield ev
            else:
                idle += 1
                if max_idle_polls is not None and idle >= max_idle_polls:
                    return
                time.sleep(self.cfg.poll_interval)


__all__ = ["LogTailer", "TailerStatus", "TailerStats"]
